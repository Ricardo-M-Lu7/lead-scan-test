from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from collections.abc import Iterator
from zoneinfo import ZoneInfo

import qrcode
import qrcode.image.svg


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "scan-events.sqlite3"
DEFAULT_QR_CODE = "onsite-test"
HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+(?::\d{1,5})?$")
DB_LOCK = threading.RLock()
AU_STATES = ("ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA")
PRODUCT_OPTIONS = (
    ("pool_wave_maker", "Pool Wave Maker"),
    ("pool_heat_pump", "Pool Heat Pump"),
    ("pool_pump", "Pool Pump"),
    ("pool_salt_chlorinator", "Pool Salt Chlorinator"),
    ("robotic_lawn_mower", "Robotic Lawn Mower"),
    ("tennis_ball_machine", "Tennis Ball Machine"),
)
PRODUCT_LABELS = dict(PRODUCT_OPTIONS)
PRODUCT_SEPARATOR = "; "
MAX_FORM_BODY_BYTES = 20_000
FIELD_LIMITS = {
    "full_name": 160,
    "phone_number": 32,
    "street_address": 200,
    "suburb": 100,
    "state_territory": 3,
    "postcode": 4,
    "intended_product": 200,
    "other_requirements": 2_000,
}
LEAD_STATUSES = ("new", "contacted", "qualified", "closed")
LEADS_PER_PAGE = 25
MAX_ADMIN_SEARCH_CHARS = 100
ADMIN_SESSION_COOKIE = "advinsys_admin_session"
ADMIN_SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
SYDNEY_TIME_ZONE = ZoneInfo("Australia/Sydney")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def initialise_database() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with database_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS qr_codes (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                destination_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'paused')),
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_events (
                id TEXT PRIMARY KEY,
                qr_code TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('qr_opened', 'landing_viewed')),
                occurred_at TEXT NOT NULL,
                visitor_id TEXT,
                ip_hash TEXT,
                user_agent TEXT,
                FOREIGN KEY(qr_code) REFERENCES qr_codes(code)
            );

            CREATE TABLE IF NOT EXISTS leads (
                id TEXT PRIMARY KEY,
                qr_code TEXT NOT NULL,
                full_name TEXT NOT NULL,
                phone_number TEXT NOT NULL,
                street_address TEXT NOT NULL,
                suburb TEXT NOT NULL,
                state_territory TEXT NOT NULL,
                postcode TEXT NOT NULL,
                intended_product TEXT NOT NULL,
                other_requirements TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'contacted', 'qualified', 'closed')),
                status_updated_at TEXT NOT NULL,
                FOREIGN KEY(qr_code) REFERENCES qr_codes(code)
            );
            """
        )
        lead_columns = {row["name"] for row in connection.execute("PRAGMA table_info(leads)")}
        if "status" not in lead_columns:
            connection.execute(
                "ALTER TABLE leads ADD COLUMN status TEXT NOT NULL DEFAULT 'new' "
                "CHECK(status IN ('new', 'contacted', 'qualified', 'closed'))"
            )
        if "status_updated_at" not in lead_columns:
            connection.execute("ALTER TABLE leads ADD COLUMN status_updated_at TEXT")
            connection.execute(
                "UPDATE leads SET status_updated_at = submitted_at WHERE status_updated_at IS NULL"
            )
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_leads_submitted_at ON leads(submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_leads_qr_code ON leads(qr_code);
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO qr_codes (code, name, destination_path, status, created_at)
            VALUES (?, ?, ?, 'active', ?)
            """,
            (DEFAULT_QR_CODE, "On-site QR redirect test", f"/entry/{DEFAULT_QR_CODE}", utc_now()),
        )


def connect_db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def database_connection() -> Iterator[sqlite3.Connection]:
    connection = connect_db()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def get_qr_code(code: str) -> sqlite3.Row | None:
    with DB_LOCK, database_connection() as connection:
        return connection.execute(
            "SELECT code, name, destination_path, status FROM qr_codes WHERE code = ?",
            (code,),
        ).fetchone()


def event_count(code: str, event_type: str) -> int:
    with DB_LOCK, database_connection() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM scan_events WHERE qr_code = ? AND event_type = ?",
                (code, event_type),
            ).fetchone()[0]
        )


def record_event(code: str, event_type: str, visitor_id: str, client_ip: str, user_agent: str) -> None:
    ip_hash = hashlib.sha256(client_ip.encode("utf-8")).hexdigest() if client_ip else ""
    with DB_LOCK, database_connection() as connection:
        connection.execute(
            """
            INSERT INTO scan_events (id, qr_code, event_type, occurred_at, visitor_id, ip_hash, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (secrets.token_urlsafe(18), code, event_type, utc_now(), visitor_id, ip_hash, user_agent[:500]),
        )


def create_lead(code: str, values: dict[str, str]) -> str:
    lead_id = f"LD-{secrets.token_urlsafe(8).upper()}"
    submitted_at = utc_now()
    with DB_LOCK, database_connection() as connection:
        connection.execute(
            """
            INSERT INTO leads (
                id, qr_code, full_name, phone_number, street_address, suburb,
                state_territory, postcode, intended_product, other_requirements, submitted_at,
                status, status_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                lead_id,
                code,
                values["full_name"],
                values["phone_number"],
                values["street_address"],
                values["suburb"],
                values["state_territory"],
                values["postcode"],
                values["intended_product"],
                values["other_requirements"],
                submitted_at,
                submitted_at,
            ),
        )
    return lead_id


def get_lead(lead_id: str) -> sqlite3.Row | None:
    with DB_LOCK, database_connection() as connection:
        return connection.execute(
            """
            SELECT id, qr_code, full_name, phone_number, street_address, suburb,
                   state_territory, postcode, intended_product, other_requirements, submitted_at,
                   status, status_updated_at
            FROM leads WHERE id = ?
            """,
            (lead_id,),
        ).fetchone()


def list_leads(limit: int, code: str | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT id, qr_code, full_name, phone_number, street_address, suburb,
               state_territory, postcode, intended_product, other_requirements, submitted_at,
               status, status_updated_at
        FROM leads
    """
    params: tuple[object, ...] = (limit,)
    if code:
        query += " WHERE qr_code = ?"
        params = (code, limit)
    query += " ORDER BY submitted_at DESC LIMIT ?"
    with DB_LOCK, database_connection() as connection:
        return connection.execute(query, params).fetchall()


def clean_admin_filters(query: dict[str, list[str]]) -> dict[str, str]:
    filters = {
        "search": query.get("search", [""])[0].strip()[:MAX_ADMIN_SEARCH_CHARS],
        "status": query.get("status", [""])[0].strip().lower(),
        "state": query.get("state", [""])[0].strip().upper(),
        "date_from": query.get("date_from", [""])[0].strip(),
        "date_to": query.get("date_to", [""])[0].strip(),
    }
    if filters["status"] not in LEAD_STATUSES:
        filters["status"] = ""
    if filters["state"] not in AU_STATES:
        filters["state"] = ""
    for field in ("date_from", "date_to"):
        if filters[field]:
            try:
                datetime.strptime(filters[field], "%Y-%m-%d")
            except ValueError:
                filters[field] = ""
    return filters


def sydney_day_start_utc(day_text: str) -> str:
    local_day = datetime.strptime(day_text, "%Y-%m-%d").date()
    return datetime.combine(local_day, datetime_time.min, SYDNEY_TIME_ZONE).astimezone(timezone.utc).isoformat()


def sydney_day_after_utc(day_text: str) -> str:
    local_day = datetime.strptime(day_text, "%Y-%m-%d").date() + timedelta(days=1)
    return datetime.combine(local_day, datetime_time.min, SYDNEY_TIME_ZONE).astimezone(timezone.utc).isoformat()


def lead_filter_sql(filters: dict[str, str]) -> tuple[str, list[object]]:
    conditions: list[str] = []
    params: list[object] = []
    if filters["search"]:
        needle = f"%{filters['search']}%"
        conditions.append(
            "(full_name LIKE ? COLLATE NOCASE OR phone_number LIKE ? COLLATE NOCASE "
            "OR intended_product LIKE ? COLLATE NOCASE OR other_requirements LIKE ? COLLATE NOCASE)"
        )
        params.extend((needle, needle, needle, needle))
    if filters["status"]:
        conditions.append("status = ?")
        params.append(filters["status"])
    if filters["state"]:
        conditions.append("state_territory = ?")
        params.append(filters["state"])
    if filters["date_from"]:
        conditions.append("submitted_at >= ?")
        params.append(sydney_day_start_utc(filters["date_from"]))
    if filters["date_to"]:
        conditions.append("submitted_at < ?")
        params.append(sydney_day_after_utc(filters["date_to"]))
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, params


def lead_count(filters: dict[str, str]) -> int:
    where, params = lead_filter_sql(filters)
    with DB_LOCK, database_connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM leads{where}", params).fetchone()[0])


def list_admin_leads(filters: dict[str, str], page: int) -> list[sqlite3.Row]:
    where, params = lead_filter_sql(filters)
    params.extend((LEADS_PER_PAGE, (page - 1) * LEADS_PER_PAGE))
    query = f"""
        SELECT id, qr_code, full_name, phone_number, street_address, suburb,
               state_territory, postcode, intended_product, other_requirements, submitted_at,
               status, status_updated_at
        FROM leads{where}
        ORDER BY submitted_at DESC
        LIMIT ? OFFSET ?
    """
    with DB_LOCK, database_connection() as connection:
        return connection.execute(query, params).fetchall()


def list_filtered_leads(filters: dict[str, str]) -> list[sqlite3.Row]:
    where, params = lead_filter_sql(filters)
    query = f"""
        SELECT id, qr_code, full_name, phone_number, street_address, suburb,
               state_territory, postcode, intended_product, other_requirements, submitted_at,
               status, status_updated_at
        FROM leads{where}
        ORDER BY submitted_at DESC
    """
    with DB_LOCK, database_connection() as connection:
        return connection.execute(query, params).fetchall()


def admin_summary_counts() -> dict[str, int]:
    now = datetime.now(SYDNEY_TIME_ZONE)
    today_start = datetime.combine(now.date(), datetime_time.min, SYDNEY_TIME_ZONE).astimezone(timezone.utc).isoformat()
    tomorrow_start = datetime.combine(now.date() + timedelta(days=1), datetime_time.min, SYDNEY_TIME_ZONE).astimezone(timezone.utc).isoformat()
    with DB_LOCK, database_connection() as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END) AS new_count,
                SUM(CASE WHEN submitted_at >= ? AND submitted_at < ? THEN 1 ELSE 0 END) AS today_count
            FROM leads
            """,
            (today_start, tomorrow_start),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "new": int(row["new_count"] or 0),
        "today": int(row["today_count"] or 0),
    }


def update_lead_status(lead_id: str, status: str) -> bool:
    if status not in LEAD_STATUSES:
        return False
    with DB_LOCK, database_connection() as connection:
        result = connection.execute(
            "UPDATE leads SET status = ?, status_updated_at = ? WHERE id = ?",
            (status, utc_now(), lead_id),
        )
    return result.rowcount == 1


def admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "")


def admin_session_secret() -> str:
    return os.environ.get("ADMIN_SESSION_SECRET", "")


def admin_access_configured() -> bool:
    return bool(admin_password() and admin_session_secret())


def sign_admin_value(value: str) -> str:
    secret = admin_session_secret().encode("utf-8")
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def create_admin_session() -> str:
    expires_at = str(int(time.time()) + ADMIN_SESSION_MAX_AGE_SECONDS)
    nonce = secrets.token_urlsafe(24)
    value = f"{expires_at}.{nonce}"
    return f"{value}.{sign_admin_value(value)}"


def parse_admin_session(token: str) -> str | None:
    if not admin_access_configured():
        return None
    try:
        expires_at, nonce, signature = token.split(".", 2)
        if int(expires_at) < int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    value = f"{expires_at}.{nonce}"
    if not hmac.compare_digest(signature, sign_admin_value(value)):
        return None
    return nonce


def csrf_token_for_session(session_nonce: str) -> str:
    return sign_admin_value(f"csrf.{session_nonce}")


def get_cookie_value(cookie: str, name: str) -> str:
    match = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]+)", cookie)
    return match.group(1) if match else ""


def format_sydney_datetime(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value).astimezone(SYDNEY_TIME_ZONE)
    except ValueError:
        return value
    return timestamp.strftime("%d %b %Y, %I:%M %p")


def status_label(status: str) -> str:
    return status.title()


def admin_filter_query(filters: dict[str, str], page: int | None = None) -> str:
    values = {key: value for key, value in filters.items() if value}
    if page and page > 1:
        values["page"] = str(page)
    return urlencode(values)


def clean_form_value(values: dict[str, str | list[str]], field: str) -> str:
    raw_value = values.get(field, "")
    if isinstance(raw_value, list):
        return raw_value[0].strip() if raw_value else ""
    return raw_value.strip()


def normalize_product_selection(raw_value: str | list[str]) -> tuple[str, bool]:
    candidates = raw_value if isinstance(raw_value, list) else [raw_value]
    selected_codes: set[str] = set()
    has_invalid_value = False
    for candidate in candidates:
        code = candidate.strip()
        if not code:
            continue
        if code not in PRODUCT_LABELS:
            has_invalid_value = True
            continue
        selected_codes.add(code)
    labels = PRODUCT_SEPARATOR.join(
        label for code, label in PRODUCT_OPTIONS if code in selected_codes
    )
    return labels, has_invalid_value


def validate_lead_form(raw_values: dict[str, str | list[str]]) -> tuple[dict[str, str], dict[str, str]]:
    values = {field: clean_form_value(raw_values, field) for field in FIELD_LIMITS}
    product_value, has_invalid_product = normalize_product_selection(raw_values.get("intended_product", ""))
    values["intended_product"] = product_value
    values["state_territory"] = values["state_territory"].upper()
    errors: dict[str, str] = {}

    required_fields = {
        "full_name": "Enter your full name.",
        "phone_number": "Enter your phone number.",
        "street_address": "Enter your street address.",
        "suburb": "Enter your suburb.",
        "state_territory": "Select your state or territory.",
        "postcode": "Enter your postcode.",
        "intended_product": "Describe the product you are interested in.",
    }
    for field, message in required_fields.items():
        if not values[field]:
            errors[field] = message
    if has_invalid_product:
        errors["intended_product"] = "Select products from the list."

    phone_compact = re.sub(r"[\s()-]", "", values["phone_number"])
    if values["phone_number"] and not re.fullmatch(r"\+?\d{8,15}", phone_compact):
        errors["phone_number"] = "Enter a valid phone number with 8 to 15 digits."
    if values["state_territory"] and values["state_territory"] not in AU_STATES:
        errors["state_territory"] = "Select an Australian state or territory."
    if values["postcode"] and not re.fullmatch(r"\d{4}", values["postcode"]):
        errors["postcode"] = "Enter a 4-digit Australian postcode."

    for field, limit in FIELD_LIMITS.items():
        raw_value = raw_values.get(field, "")
        if isinstance(raw_value, list):
            raw_value = PRODUCT_SEPARATOR.join(value.strip() for value in raw_value)
        else:
            raw_value = raw_value.strip()
        if len(raw_value) > limit and field not in errors:
            errors[field] = f"Keep this field within {limit} characters."
    return values, errors


def configured_public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")


def request_base_url(handler: BaseHTTPRequestHandler) -> str:
    configured = configured_public_base_url()
    if configured:
        return configured

    host = handler.headers.get("Host", "")
    if not HOST_PATTERN.fullmatch(host):
        return "http://127.0.0.1:8082"

    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    scheme = "https" if forwarded_proto == "https" else "http"
    return f"{scheme}://{host}"


def make_qr_svg(target_url: str) -> bytes:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=12, border=4)
    qr.add_data(target_url)
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    output = io.BytesIO()
    image.save(output)
    return output.getvalue()


def html_page(title: str, body: str) -> bytes:
    document = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <meta name=\"theme-color\" content=\"#0f5a44\">
  <title>{html.escape(title)}</title>
  <link rel=\"stylesheet\" href=\"/assets/app.css\">
</head>
<body>{body}</body>
</html>"""
    return document.encode("utf-8")


def dashboard_page(base_url: str, code: str, record: sqlite3.Row) -> bytes:
    target = f"{base_url}/q/{code}"
    body = f"""
<main class=\"shell\">
  <section class=\"card\">
    <p class=\"eyebrow\">ADVINSYS CUSTOMER ENQUIRY</p>
    <h1>Scan this code with a phone</h1>
    <p class=\"lead\">The code opens the customer enquiry form and records an anonymous scan event.</p>
    <img class=\"qr\" src=\"/qr/{html.escape(code)}.svg\" alt=\"QR code for the on-site redirect test\">
    <p class=\"label\">QR target</p>
    <code>{html.escape(target)}</code>
    <p class=\"note\">Keep this browser on the public HTTPS address while scanning. A local localhost address cannot be opened by another phone.</p>
  </section>
</main>"""
    return html_page(f"{record['name']} | QR Test", body)


def legacy_entry_page(code: str, record: sqlite3.Row) -> bytes:
    body = f"""
<main class=\"shell\">
  <section class=\"card result\">
    <div class=\"check\" aria-hidden=\"true\">✓</div>
    <p class=\"eyebrow\">ON-SITE CUSTOMER JOURNEY</p>
    <h1>Scan and redirect successful</h1>
    <p class=\"lead\">Your phone has reached the customer landing page.</p>
    <dl class=\"details\">
      <div><dt>Test source</dt><dd>{html.escape(record['name'])}</dd></div>
      <div><dt>QR code</dt><dd>{html.escape(code)}</dd></div>
    </dl>
    <p class=\"note\">Registration fields, material upload, and customer communication will be added after the business requirements are confirmed.</p>
  </section>
</main>"""
    return html_page("Scan successful | QR Test", body)


def field_error(field: str, errors: dict[str, str]) -> str:
    message = errors.get(field)
    if not message:
        return ""
    return f'<div class="field-error" id="error-{field}" role="alert">{html.escape(message)}</div>'


def text_field(
    label: str,
    field: str,
    values: dict[str, str],
    errors: dict[str, str],
    *,
    autocomplete: str = "",
    input_type: str = "text",
    placeholder: str = "",
) -> str:
    value = html.escape(values.get(field, ""), quote=True)
    invalid = ' aria-invalid="true" aria-describedby="error-' + field + '"' if field in errors else ""
    autocomplete_attr = f' autocomplete="{html.escape(autocomplete, quote=True)}"' if autocomplete else ""
    return f"""
    <label class="field-card">
      <span class="field-label">{html.escape(label)}</span>
      <input class="field-input" type="{input_type}" name="{field}" value="{value}"{autocomplete_attr}
        placeholder="{html.escape(placeholder, quote=True)}" required{invalid}>
      {field_error(field, errors)}
    </label>"""


def textarea_field(label: str, field: str, values: dict[str, str], errors: dict[str, str]) -> str:
    invalid = ' aria-invalid="true" aria-describedby="error-' + field + '"' if field in errors else ""
    value = html.escape(values.get(field, ""))
    return f"""
    <label class="field-card field-card-wide">
      <span class="field-label">{html.escape(label)} <span class="optional">Optional</span></span>
      <textarea class="field-textarea" name="{field}" rows="5" placeholder="Tell us anything else that would help us assist you."{invalid}>{value}</textarea>
      {field_error(field, errors)}
    </label>"""


def state_field(values: dict[str, str], errors: dict[str, str]) -> str:
    selected_state = values.get("state_territory", "")
    placeholder_selected = " selected" if selected_state not in AU_STATES else ""
    options = [f'<option value="" disabled{placeholder_selected}>Select state or territory</option>']
    options.extend(
        f'<option value="{state}"{" selected" if state == selected_state else ""}>{state}</option>'
        for state in AU_STATES
    )
    invalid = ' aria-invalid="true" aria-describedby="error-state_territory"' if "state_territory" in errors else ""
    return f"""
    <label class="field-card">
      <span class="field-label">State / Territory</span>
      <select class="field-select" name="state_territory" autocomplete="address-level1" required{invalid}>
        {''.join(options)}
      </select>
      {field_error("state_territory", errors)}
    </label>"""


def product_field(values: dict[str, str], errors: dict[str, str]) -> str:
    selected_labels = {
        value.strip()
        for value in values.get("intended_product", "").split(PRODUCT_SEPARATOR)
        if value.strip()
    }
    selected_summary = ", ".join(
        label for _code, label in PRODUCT_OPTIONS if label in selected_labels
    )
    summary_text = selected_summary or "Select one or more products"
    invalid = ' aria-invalid="true" aria-describedby="error-intended_product"' if "intended_product" in errors else ""
    options = "".join(
        f'''<label class="product-option"><input type="checkbox" name="intended_product" value="{html.escape(code, quote=True)}" data-label="{html.escape(label, quote=True)}"{" checked" if label in selected_labels else ""}><span>{html.escape(label)}</span></label>'''
        for code, label in PRODUCT_OPTIONS
    )
    return f"""
    <div class="field-card field-card-wide product-field">
      <span class="field-label">Intended Product</span>
      <details class="product-picker" data-product-picker aria-required="true"{invalid}>
        <summary class="field-select product-picker-summary" data-product-summary>{html.escape(summary_text)}</summary>
        <div class="product-picker-menu" role="group" aria-label="Product options">
          {options}
        </div>
      </details>
      {field_error("intended_product", errors)}
    </div>"""


def entry_page(
    code: str,
    record: sqlite3.Row,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
) -> bytes:
    values = values or {}
    errors = errors or {}
    form_title = "Please correct the highlighted fields." if errors else "Complete the form and our team will contact you."
    body = f"""
<div class="form-shell">
  <aside class="sidebar" aria-label="Enquiry progress">
    <div class="sidebar-inner">
      <div class="side-card">
        <p class="side-kicker">ADVINSYS</p>
        <h2 class="side-title">Customer enquiry</h2>
        <p class="side-copy">Share your details and product interest with our team.</p>
        <div class="progress" aria-label="Step 2 of 3">
          <div class="progress-meta"><span>Progress</span><span>2 of 3</span></div>
          <div class="progress-bar"><span style="width: 66%"></span></div>
        </div>
      </div>
      <nav class="nav" aria-label="Enquiry steps">
        <div class="nav-item is-done"><span class="nav-num">1</span><span><strong class="nav-title">Scan complete</strong><small class="nav-sub">You are connected to Advinsys.</small></span></div>
        <div class="nav-item is-current"><span class="nav-num">2</span><span><strong class="nav-title">Your details</strong><small class="nav-sub">Tell us how we can help.</small></span></div>
        <div class="nav-item"><span class="nav-num">3</span><span><strong class="nav-title">Confirmation</strong><small class="nav-sub">We will record your enquiry.</small></span></div>
      </nav>
    </div>
  </aside>
  <div class="content">
    <header class="topbar"><div class="brand-name">ADVINSYS</div><div class="topbar-right">Customer enquiry</div></header>
    <main class="app">
      <section class="page">
        <header class="page-banner">
          <div><p class="eyebrow">CUSTOMER ENQUIRY</p><h1>How can we help?</h1></div>
          <p class="lead">{form_title}</p>
        </header>
        <form class="panel" method="post" action="/entry/{html.escape(code, quote=True)}" novalidate>
          <div class="section-head"><span class="section-num">01</span><h2>Contact details</h2></div>
          <div class="grid grid-2">
            {text_field("Full Name", "full_name", values, errors, autocomplete="name", placeholder="Your full name")}
            {text_field("Phone Number", "phone_number", values, errors, autocomplete="tel", input_type="tel", placeholder="e.g. 04XX XXX XXX")}
          </div>
          <div class="section-head section-head-spaced"><span class="section-num">02</span><h2>Address</h2></div>
          <div class="grid grid-2">
            {text_field("Street Address", "street_address", values, errors, autocomplete="street-address", placeholder="Street number and name")}
            {text_field("Suburb", "suburb", values, errors, autocomplete="address-level2", placeholder="Your suburb")}
            {state_field(values, errors)}
            {text_field("Postcode", "postcode", values, errors, autocomplete="postal-code", input_type="text", placeholder="4-digit postcode")}
          </div>
          <div class="section-head section-head-spaced"><span class="section-num">03</span><h2>Product interest</h2></div>
          <div class="grid grid-2">
            {product_field(values, errors)}
            {textarea_field("Other Requirements", "other_requirements", values, errors)}
          </div>
          <div class="footer-actions"><div class="footer-actions-inner"><span class="form-note">Fields marked as required must be completed.</span><button class="button button-primary" type="submit">Submit enquiry</button></div></div>
        </form>
        <script>
          (() => {{
            const picker = document.querySelector("[data-product-picker]");
            if (!picker) return;
            const summary = picker.querySelector("[data-product-summary]");
            const boxes = Array.from(picker.querySelectorAll('input[name="intended_product"]'));
            const placeholder = "Select one or more products";
            const updateSummary = () => {{
              const selected = boxes.filter((box) => box.checked).map((box) => box.dataset.label);
              summary.textContent = selected.length ? selected.join(", ") : placeholder;
            }};
            boxes.forEach((box) => box.addEventListener("change", updateSummary));
            updateSummary();
          }})();
        </script>
      </section>
    </main>
    <footer class="page-footer"><span>ADVINSYS</span><span>CUSTOMER ENQUIRY</span></footer>
  </div>
</div>"""
    return html_page("Customer enquiry | Advinsys", body)


def thanks_page(lead: sqlite3.Row) -> bytes:
    body = f"""
<div class="form-shell">
  <aside class="sidebar"><div class="sidebar-inner"><div class="side-card"><p class="side-kicker">ADVINSYS</p><h2 class="side-title">Customer enquiry</h2><p class="side-copy">Your enquiry has been recorded.</p><div class="progress"><div class="progress-meta"><span>Progress</span><span>3 of 3</span></div><div class="progress-bar"><span style="width: 100%"></span></div></div></div></div></aside>
  <div class="content"><header class="topbar"><div class="brand-name">ADVINSYS</div><div class="topbar-right">Customer enquiry</div></header><main class="app"><section class="page"><div class="panel confirmation"><div class="confirmation-icon" aria-hidden="true">✓</div><p class="eyebrow">ENQUIRY SUBMITTED</p><h1>Thank you, {html.escape(lead['full_name'])}.</h1><p class="lead">Your enquiry has been recorded. Our team will use the contact details you provided to follow up.</p><dl class="details"><div><dt>Reference number</dt><dd>{html.escape(lead['id'])}</dd></div><div><dt>Product interest</dt><dd>{html.escape(lead['intended_product'])}</dd></div></dl></div></section></main><footer class="page-footer"><span>ADVINSYS</span><span>CUSTOMER ENQUIRY</span></footer></div>
</div>"""
    return html_page("Enquiry submitted | Advinsys", body)


def status_options(selected: str, include_all: bool = False) -> str:
    options: list[tuple[str, str]] = [("", "All statuses")] if include_all else []
    options.extend((status, status_label(status)) for status in LEAD_STATUSES)
    return "".join(
        f'<option value="{value}"{" selected" if value == selected else ""}>{html.escape(label)}</option>'
        for value, label in options
    )


def state_options(selected: str) -> str:
    options = ['<option value="">All states and territories</option>']
    options.extend(
        f'<option value="{state}"{" selected" if state == selected else ""}>{state}</option>'
        for state in AU_STATES
    )
    return "".join(options)


def admin_layout(title: str, content: str, session_nonce: str, active: str = "leads") -> bytes:
    csrf_token = csrf_token_for_session(session_nonce)
    all_leads_class = " is-active" if active == "leads" else ""
    body = f"""
<div class="admin-shell">
  <aside class="admin-sidebar" aria-label="Lead management navigation">
    <div class="admin-sidebar-inner">
      <a class="admin-brand" href="/admin"><span>ADVINSYS</span><strong>Lead Management</strong></a>
      <nav class="admin-nav" aria-label="Primary navigation">
        <a class="admin-nav-item{all_leads_class}" href="/admin">All Leads</a>
      </nav>
      <form class="admin-logout" method="post" action="/admin/logout"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}"><button class="admin-text-button" type="submit">Sign out</button></form>
    </div>
  </aside>
  <div class="admin-content">
    <header class="admin-topbar"><span>ADVINSYS</span><span>Lead Management</span></header>
    <main class="admin-main">{content}</main>
  </div>
</div>"""
    return html_page(title, body)


def admin_login_page(error: str = "") -> bytes:
    error_markup = f'<p class="admin-login-error" role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""
<main class="admin-login-shell">
  <section class="admin-login-card" aria-labelledby="admin-login-title">
    <p class="eyebrow">ADVINSYS</p>
    <h1 id="admin-login-title">Lead Management</h1>
    <p class="lead">Sign in to view and manage customer enquiries.</p>
    {error_markup}
    <form method="post" action="/admin/login">
      <label class="admin-login-field"><span>Administrator password</span><span class="admin-password-control"><input id="admin-password" type="password" name="password" autocomplete="current-password" required autofocus><button id="admin-password-toggle" class="admin-password-toggle" type="button" aria-controls="admin-password" aria-label="Show password" aria-pressed="false" title="Show password"><svg class="admin-password-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path><circle cx="12" cy="12" r="2.5" fill="none" stroke="currentColor" stroke-width="2"></circle></svg></button></span></label>
      <button class="button button-primary" type="submit">Sign in</button>
    </form>
    <script>
      (() => {{
        const password = document.getElementById("admin-password");
        const toggle = document.getElementById("admin-password-toggle");
        if (!password || !toggle) return;
        const eyeIcon = '<svg class="admin-password-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path><circle cx="12" cy="12" r="2.5" fill="none" stroke="currentColor" stroke-width="2"></circle></svg>';
        const eyeOffIcon = '<svg class="admin-password-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m3 3 18 18M10.6 10.6a2.5 2.5 0 0 0 3.5 3.5M9.9 5.8A10.3 10.3 0 0 1 12 5.6c6 0 9.5 6.4 9.5 6.4a17 17 0 0 1-3 3.8M6.2 6.2A17.5 17.5 0 0 0 2.5 12s3.5 6.4 9.5 6.4a10 10 0 0 0 2.2-.3" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></path></svg>';
        toggle.addEventListener("click", () => {{
          const reveal = password.type === "password";
          password.type = reveal ? "text" : "password";
          toggle.innerHTML = reveal ? eyeOffIcon : eyeIcon;
          toggle.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
          toggle.setAttribute("title", reveal ? "Hide password" : "Show password");
          toggle.setAttribute("aria-pressed", String(reveal));
          password.focus();
        }});
      }})();
    </script>
  </section>
</main>"""
    return html_page("Lead Management sign in | Advinsys", body)


def admin_stats_markup(summary: dict[str, int]) -> str:
    cards = (
        ("New leads", summary["new"], "Awaiting follow-up"),
        ("All leads", summary["total"], "Recorded enquiries"),
        ("Today", summary["today"], "Australia/Sydney time"),
    )
    return "".join(
        f'<section class="admin-stat"><span>{label}</span><strong>{value}</strong><small>{helper}</small></section>'
        for label, value, helper in cards
    )


def admin_lead_list_page(filters: dict[str, str], page: int, session_nonce: str) -> bytes:
    total = lead_count(filters)
    last_page = max(1, (total + LEADS_PER_PAGE - 1) // LEADS_PER_PAGE)
    page = min(max(page, 1), last_page)
    leads = list_admin_leads(filters, page)
    rows = "".join(
        f"""
        <tr>
          <td data-label="Submitted">{html.escape(format_sydney_datetime(lead['submitted_at']))}</td>
          <td data-label="Name"><a class="admin-lead-link" href="/admin/leads/{html.escape(lead['id'], quote=True)}">{html.escape(lead['full_name'])}</a></td>
          <td data-label="Phone">{html.escape(lead['phone_number'])}</td>
          <td data-label="Location">{html.escape(lead['suburb'])}, {html.escape(lead['state_territory'])} {html.escape(lead['postcode'])}</td>
          <td data-label="Product">{html.escape(lead['intended_product'])}</td>
          <td data-label="Status"><span class="lead-status lead-status-{html.escape(lead['status'], quote=True)}">{html.escape(status_label(lead['status']))}</span></td>
        </tr>"""
        for lead in leads
    )
    if not rows:
        rows = '<tr><td class="admin-empty" colspan="6">No leads match these filters.</td></tr>'
    previous_href = f"/admin?{admin_filter_query(filters, page - 1)}" if page > 1 else ""
    next_href = f"/admin?{admin_filter_query(filters, page + 1)}" if page < last_page else ""
    previous = f'<a class="button admin-button-secondary" href="{html.escape(previous_href, quote=True)}">Previous</a>' if previous_href else '<span class="button admin-button-disabled">Previous</span>'
    next_link = f'<a class="button admin-button-secondary" href="{html.escape(next_href, quote=True)}">Next</a>' if next_href else '<span class="button admin-button-disabled">Next</span>'
    export_query = admin_filter_query(filters)
    export_href = "/admin/export.csv" + (f"?{export_query}" if export_query else "")
    content = f"""
<section class="admin-page-head"><div><p class="eyebrow">ADVINSYS</p><h1>Lead Management</h1><p class="lead">Review and follow up customer enquiries.</p></div><a class="button button-primary" href="{html.escape(export_href, quote=True)}">Export CSV</a></section>
<section class="admin-stats-grid">{admin_stats_markup(admin_summary_counts())}</section>
<form class="admin-filters" method="get" action="/admin">
  <label><span>Search</span><input type="search" name="search" value="{html.escape(filters['search'], quote=True)}" placeholder="Name, phone, product, or requirement"></label>
  <label><span>Status</span><select name="status">{status_options(filters['status'], include_all=True)}</select></label>
  <label><span>State / Territory</span><select name="state">{state_options(filters['state'])}</select></label>
  <label><span>From</span><input type="date" name="date_from" value="{html.escape(filters['date_from'], quote=True)}"></label>
  <label><span>To</span><input type="date" name="date_to" value="{html.escape(filters['date_to'], quote=True)}"></label>
  <div class="admin-filter-actions"><button class="button button-primary" type="submit">Apply filters</button><a class="admin-clear-link" href="/admin">Clear filters</a></div>
</form>
<section class="admin-table-section" aria-label="Leads">
  <div class="admin-table-meta"><strong>{total} lead{'s' if total != 1 else ''}</strong><span>Page {page} of {last_page}</span></div>
  <div class="admin-table-wrap"><table class="admin-leads-table"><thead><tr><th>Submitted</th><th>Name</th><th>Phone</th><th>Location</th><th>Product</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></div>
  <nav class="admin-pagination" aria-label="Lead pages">{previous}<span>Page {page} of {last_page}</span>{next_link}</nav>
</section>"""
    return admin_layout("Lead Management | Advinsys", content, session_nonce)


def admin_lead_detail_page(lead: sqlite3.Row, session_nonce: str, updated: bool = False) -> bytes:
    update_message = '<p class="admin-success" role="status">Lead status updated.</p>' if updated else ""
    csrf_token = csrf_token_for_session(session_nonce)
    address_lines = "<br>".join(
        html.escape(value)
        for value in (
            lead["street_address"],
            f"{lead['suburb']}, {lead['state_territory']} {lead['postcode']}",
        )
    )
    requirements = html.escape(lead["other_requirements"]) if lead["other_requirements"] else "Not provided"
    content = f"""
<section class="admin-detail-head"><div><a class="button admin-button-secondary admin-back-link" href="/admin">Back to all leads</a><p class="eyebrow">LEAD DETAIL</p><h1>{html.escape(lead['full_name'])}</h1><p class="lead">Submitted {html.escape(format_sydney_datetime(lead['submitted_at']))}</p></div><span class="lead-status lead-status-{html.escape(lead['status'], quote=True)}">{html.escape(status_label(lead['status']))}</span></section>
{update_message}
<div class="admin-detail-grid">
  <section class="admin-detail-section"><h2>Contact details</h2><dl><div><dt>Full name</dt><dd>{html.escape(lead['full_name'])}</dd></div><div><dt>Phone number</dt><dd>{html.escape(lead['phone_number'])}</dd></div><div><dt>Address</dt><dd>{address_lines}</dd></div></dl></section>
  <section class="admin-detail-section"><h2>Enquiry</h2><dl><div><dt>Intended product</dt><dd>{html.escape(lead['intended_product'])}</dd></div><div><dt>Other requirements</dt><dd class="admin-multiline">{requirements}</dd></div><div><dt>QR source</dt><dd>{html.escape(lead['qr_code'])}</dd></div><div><dt>Reference</dt><dd>{html.escape(lead['id'])}</dd></div></dl></section>
  <section class="admin-detail-section admin-status-editor"><h2>Lead status</h2><p>Update the status after each follow-up.</p><form method="post" action="/admin/leads/{html.escape(lead['id'], quote=True)}/status"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}"><label><span>Status</span><select name="status">{status_options(lead['status'])}</select></label><button class="button button-primary" type="submit">Update status</button></form></section>
</div>"""
    return admin_layout(f"{lead['full_name']} | Lead Management", content, session_nonce)


CSS = """
:root { --ink:#18352d; --muted:#5e706a; --line:#cfe0d7; --brand:#0f5a44; --accent:#8cc63e; --surface:#fff; --canvas:#f3f7f3; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,\"Segoe UI\",Arial,sans-serif; }
* { box-sizing:border-box; }
body { min-width:320px; min-height:100vh; margin:0; color:var(--ink); background:var(--canvas); }
.shell { display:grid; min-height:100vh; place-items:center; padding:24px; }
.card { width:min(100%,540px); padding:36px; border:1px solid var(--line); border-radius:8px; background:var(--surface); box-shadow:0 18px 44px rgba(15,90,68,.10); text-align:center; }
.eyebrow { margin:0 0 12px; color:var(--brand); font-size:12px; font-weight:700; letter-spacing:0; }
h1 { margin:0; font-size:28px; line-height:1.25; }
.lead { margin:16px auto 24px; color:var(--muted); font-size:16px; line-height:1.55; }
.qr { display:block; width:min(100%,280px); height:auto; margin:0 auto 22px; padding:12px; border:1px solid var(--line); background:#fff; }
.label { margin:0 0 6px; color:var(--muted); font-size:12px; font-weight:700; }
code { display:block; overflow-wrap:anywhere; padding:10px 12px; border-radius:4px; background:#f0f6f1; color:var(--brand); font-size:12px; line-height:1.45; text-align:left; }
.note { margin:20px 0 0; color:var(--muted); font-size:13px; line-height:1.55; }
.check { display:grid; width:54px; height:54px; margin:0 auto 18px; place-items:center; border-radius:50%; background:var(--brand); color:#fff; font-size:34px; font-weight:700; }
.details { display:grid; gap:12px; margin:26px 0 0; padding:16px; border:1px solid var(--line); border-left:4px solid var(--accent); border-radius:4px; text-align:left; }
.details div { display:grid; gap:4px; }.details dt { color:var(--muted); font-size:12px; }.details dd { margin:0; overflow-wrap:anywhere; font-size:14px; font-weight:700; }
@media (max-width:480px) { .shell { padding:16px; }.card { padding:30px 22px; }h1 { font-size:26px; } }
/* Customer enquiry form */
.form-shell { display:grid; grid-template-columns:280px minmax(0,1fr); min-height:100vh; }
.sidebar { position:sticky; top:0; height:100vh; overflow:auto; border-right:1px solid var(--line); background:#f7fbf7; }
.sidebar-inner { display:flex; flex-direction:column; min-height:100%; padding:20px; gap:18px; }
.side-card,.panel { border:1px solid var(--line); border-radius:6px; background:#fff; box-shadow:0 10px 24px rgba(7,44,33,.06); }
.side-card { padding:18px; }
.side-kicker,.eyebrow { color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0; text-transform:uppercase; }
.side-title { margin:6px 0 8px; color:var(--brand); font-size:21px; line-height:1.15; }
.side-copy,.nav-sub,.form-note { color:var(--muted); font-size:13px; line-height:1.5; }
.progress { display:grid; gap:6px; margin-top:16px; }
.progress-meta { display:flex; justify-content:space-between; gap:12px; color:var(--muted); font-size:12px; }
.progress-bar { height:8px; overflow:hidden; border-radius:999px; background:#dde6e0; }
.progress-bar span { display:block; height:100%; border-radius:inherit; background:var(--accent); }
.nav { display:grid; gap:8px; }
.nav-item { display:grid; grid-template-columns:32px minmax(0,1fr); gap:10px; align-items:start; padding:12px; border:1px solid transparent; border-radius:6px; }
.nav-item.is-current { border-color:#9fc8b8; background:#fff; box-shadow:0 8px 18px rgba(7,44,33,.05); }
.nav-num { display:grid; width:28px; height:28px; place-items:center; border-radius:50%; background:#e4ede7; color:var(--brand); font-size:12px; font-weight:800; }
.nav-item.is-done .nav-num { background:var(--accent); color:#fff; }
.nav-item.is-current .nav-num { background:var(--brand); color:#fff; }
.nav-title { display:block; color:var(--brand); font-size:14px; line-height:1.3; }
.nav-sub { display:block; margin-top:2px; }
.content { display:flex; flex-direction:column; min-width:0; min-height:100vh; }
.topbar { position:sticky; top:0; z-index:5; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:16px 24px; border-bottom:1px solid var(--line); background:rgba(243,247,243,.96); }
.brand-name { color:var(--brand); font-size:15px; font-weight:800; letter-spacing:0; }
.topbar-right { color:var(--muted); font-size:14px; }
.app { flex:1; padding:24px; }
.page { width:min(100%,1020px); margin:0 auto; }
.page-banner { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; margin-bottom:16px; padding-bottom:14px; border-bottom:2px solid var(--accent); }
.page-banner .eyebrow { margin:0; }
.page-banner h1 { margin:5px 0 0; color:var(--brand); font-size:34px; line-height:1.1; }
.page-banner .lead { max-width:440px; margin:0; text-align:right; }
.panel { padding:20px; }
.section-head { display:flex; align-items:baseline; gap:10px; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid var(--line); }
.section-head-spaced { margin-top:24px; }
.section-num { color:var(--accent); font-size:12px; font-weight:800; }
.section-head h2 { margin:0; color:var(--brand); font-size:18px; line-height:1.2; text-transform:uppercase; }
.grid { display:grid; gap:14px; }
.grid-2 { grid-template-columns:repeat(2,minmax(0,1fr)); }
.field-card { display:grid; gap:7px; padding:14px; border:1px solid var(--line); border-bottom:3px solid var(--brand); border-radius:6px; background:#fff; }
.field-card-wide { grid-column:1 / -1; }
.field-label { color:var(--brand); font-size:14px; font-weight:800; line-height:1.35; }
.optional { color:var(--muted); font-size:12px; font-weight:600; }
.field-input,.field-select,.field-textarea { width:100%; border:1px solid #2d7a5c; border-radius:6px; background:#fff; color:var(--ink); font:inherit; outline:none; }
.field-input,.field-select { min-height:42px; padding:10px 12px; }
.field-textarea { min-height:112px; padding:10px 12px; resize:vertical; }
.field-input:focus,.field-select:focus,.field-textarea:focus { border-color:var(--brand); box-shadow:0 0 0 3px rgba(15,90,68,.12); }
.product-picker { position:relative; }
.product-picker-summary { display:flex; align-items:center; justify-content:space-between; gap:12px; min-height:42px; padding:10px 12px; cursor:pointer; list-style:none; overflow-wrap:anywhere; }
.product-picker-summary::-webkit-details-marker { display:none; }
.product-picker-summary::after { flex:0 0 auto; width:8px; height:8px; border-right:2px solid var(--brand); border-bottom:2px solid var(--brand); content:""; transform:rotate(45deg) translateY(-2px); }
.product-picker[open] .product-picker-summary::after { transform:rotate(225deg) translateY(-2px); }
.product-picker[open] .product-picker-summary,.product-picker-summary:focus-visible { border-color:var(--brand); box-shadow:0 0 0 3px rgba(15,90,68,.12); outline:none; }
.product-picker-menu { display:grid; gap:6px; margin-top:8px; padding:8px; border:1px solid var(--line); border-radius:6px; background:#fff; box-shadow:0 10px 24px rgba(7,44,33,.10); }
.product-option { display:flex; align-items:center; gap:10px; padding:9px 10px; border-radius:4px; color:var(--ink); font-size:14px; font-weight:600; cursor:pointer; }
.product-option:hover { background:#eef7f2; }
.product-option input { width:18px; height:18px; margin:0; accent-color:var(--brand); }
.field-error { color:#a33a2b; font-size:12px; font-weight:700; line-height:1.35; }
.footer-actions { position:sticky; bottom:0; margin-top:24px; padding-top:10px; background:rgba(243,247,243,.96); }
.footer-actions-inner { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px; border:1px solid var(--line); border-radius:6px; background:#fff; box-shadow:0 10px 24px rgba(7,44,33,.06); }
.button { display:inline-flex; align-items:center; justify-content:center; min-height:42px; padding:11px 16px; border:0; border-radius:6px; font:inherit; font-weight:800; cursor:pointer; }
.button-primary { background:var(--brand); color:#fff; }
.button-primary:hover { background:#0b4938; }
.page-footer { display:flex; justify-content:space-between; gap:12px; padding:16px 24px 20px; color:var(--muted); font-size:12px; font-weight:700; letter-spacing:0; }
.confirmation { max-width:620px; margin:48px auto; text-align:center; }
.confirmation-icon { display:grid; width:54px; height:54px; place-items:center; margin:0 auto 16px; border-radius:50%; background:var(--accent); color:#fff; font-size:32px; font-weight:800; }
.confirmation .lead { max-width:460px; }
@media (max-width:860px) { .form-shell { grid-template-columns:1fr; }.sidebar { position:relative; height:auto; border-right:0; border-bottom:1px solid var(--line); }.sidebar-inner { min-height:auto; padding:16px; }.nav { grid-template-columns:repeat(3,minmax(0,1fr)); }.nav-item { grid-template-columns:28px minmax(0,1fr); padding:10px; }.topbar { position:relative; }.page-banner { align-items:flex-start; flex-direction:column; }.page-banner .lead { text-align:left; }.confirmation { margin:20px auto; } }
@media (max-width:620px) { .app { padding:14px; }.topbar,.page-footer { padding-left:14px; padding-right:14px; }.page-banner h1 { font-size:28px; }.grid-2 { grid-template-columns:1fr; }.field-card-wide { grid-column:auto; }.nav { grid-template-columns:1fr; }.footer-actions-inner { align-items:stretch; flex-direction:column; }.button { width:100%; }.page-footer { flex-direction:column; }.panel { padding:14px; } }
/* Lead management */
.admin-shell { display:grid; grid-template-columns:240px minmax(0,1fr); min-height:100vh; }
.admin-sidebar { position:sticky; top:0; display:flex; min-height:100vh; border-right:1px solid var(--line); background:#f7fbf7; }
.admin-sidebar-inner { display:flex; width:100%; flex-direction:column; padding:22px 16px; }
.admin-brand { display:grid; gap:5px; padding:8px 10px 24px; color:var(--brand); text-decoration:none; }
.admin-brand span { color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0; }
.admin-brand strong { font-size:20px; line-height:1.15; }
.admin-nav { display:grid; gap:6px; }
.admin-nav-item { padding:11px 12px; border:1px solid transparent; border-radius:6px; color:var(--brand); font-size:14px; font-weight:800; text-decoration:none; }
.admin-nav-item:hover,.admin-nav-item.is-active { border-color:#a5cdbd; background:#fff; }
.admin-logout { margin-top:auto; padding:16px 10px 0; }
.admin-text-button,.admin-clear-link { padding:0; border:0; background:transparent; color:var(--brand); font:inherit; font-size:14px; font-weight:800; text-decoration:none; cursor:pointer; }
.admin-content { min-width:0; }
.admin-topbar { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:16px 28px; border-bottom:1px solid var(--line); color:var(--muted); font-size:14px; }
.admin-topbar span:first-child { color:var(--brand); font-weight:800; }
.admin-main { width:min(100%,1280px); margin:0 auto; padding:28px; }
.admin-page-head,.admin-detail-head { display:flex; align-items:flex-end; justify-content:space-between; gap:20px; margin-bottom:22px; padding-bottom:16px; border-bottom:2px solid var(--accent); }
.admin-page-head .eyebrow,.admin-detail-head .eyebrow { margin:0 0 5px; }
.admin-page-head h1,.admin-detail-head h1 { margin:0; color:var(--brand); font-size:32px; line-height:1.1; }
.admin-page-head .lead,.admin-detail-head .lead { margin:7px 0 0; }
.admin-stats-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin-bottom:20px; }
.admin-stat { display:grid; gap:5px; min-height:128px; padding:18px; border:1px solid var(--line); border-radius:6px; background:#fff; box-shadow:0 8px 18px rgba(7,44,33,.04); }
.admin-stat span,.admin-stat small { color:var(--muted); font-size:13px; }
.admin-stat strong { color:var(--brand); font-size:32px; line-height:1.1; }
.admin-filters { display:grid; grid-template-columns:2fr repeat(4,minmax(125px,1fr)) auto; gap:12px; align-items:end; padding:16px; border:1px solid var(--line); border-radius:6px; background:#fff; }
.admin-filters label,.admin-status-editor label,.admin-login-field { display:grid; gap:6px; color:var(--brand); font-size:13px; font-weight:800; }
.admin-filters input,.admin-filters select,.admin-status-editor select,.admin-login-field input { min-width:0; min-height:40px; width:100%; padding:8px 10px; border:1px solid #2d7a5c; border-radius:6px; background:#fff; color:var(--ink); font:inherit; outline:none; }
.admin-filters input:focus,.admin-filters select:focus,.admin-status-editor select:focus,.admin-login-field input:focus { box-shadow:0 0 0 3px rgba(15,90,68,.12); }
.admin-filter-actions { display:flex; align-items:center; gap:10px; padding-bottom:1px; white-space:nowrap; }
.admin-filter-actions .button { min-height:40px; }
.admin-table-section { margin-top:20px; border:1px solid var(--line); border-radius:6px; background:#fff; overflow:hidden; }
.admin-table-meta,.admin-pagination { display:flex; align-items:center; justify-content:space-between; gap:14px; padding:15px 18px; color:var(--muted); font-size:13px; }
.admin-table-meta { border-bottom:1px solid var(--line); }.admin-table-meta strong { color:var(--brand); }
.admin-table-wrap { overflow-x:auto; }
.admin-leads-table { width:100%; border-collapse:collapse; min-width:920px; text-align:left; }
.admin-leads-table th { padding:12px 14px; border-bottom:1px solid var(--line); color:var(--muted); font-size:12px; font-weight:800; text-transform:uppercase; }
.admin-leads-table td { padding:15px 14px; border-bottom:1px solid #e7eee9; color:var(--ink); font-size:14px; vertical-align:top; }
.admin-leads-table tbody tr:last-child td { border-bottom:0; }
.admin-lead-link { color:var(--brand); font-weight:800; text-decoration:none; }.admin-lead-link:hover { text-decoration:underline; }
.lead-status { display:inline-flex; align-items:center; min-height:27px; padding:5px 9px; border-radius:999px; font-size:12px; font-weight:800; }
.lead-status-new { background:#eaf4e5; color:#2f6d1c; }.lead-status-contacted { background:#e5f1ed; color:#0f5a44; }.lead-status-qualified { background:#fff2cb; color:#795a00; }.lead-status-closed { background:#edf0ee; color:#56655e; }
.admin-empty { padding:28px !important; color:var(--muted) !important; text-align:center; }
.admin-pagination { border-top:1px solid var(--line); }.admin-pagination span { font-weight:700; }
.admin-button-secondary { border:1px solid var(--line); background:#fff; color:var(--brand); text-decoration:none; }.admin-button-secondary:hover { border-color:#8abda8; background:#eef7f2; }.admin-button-secondary:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }.admin-button-disabled { color:#98a39d; background:#eef2ef; cursor:default; }
.admin-back-link { margin-bottom:14px; }.admin-detail-head { align-items:flex-start; }
.admin-success { margin:0 0 16px; padding:12px 14px; border-left:6px solid var(--accent); background:#eaf4e5; color:#2f6d1c; font-weight:800; }
.admin-detail-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }.admin-detail-section { padding:20px; border:1px solid var(--line); border-radius:6px; background:#fff; }.admin-detail-section:last-child { grid-column:1 / -1; }
.admin-detail-section h2 { margin:0 0 16px; padding-bottom:10px; border-bottom:1px solid var(--line); color:var(--brand); font-size:18px; }.admin-detail-section dl { display:grid; gap:15px; margin:0; }.admin-detail-section dt { color:var(--muted); font-size:12px; font-weight:800; text-transform:uppercase; }.admin-detail-section dd { margin:5px 0 0; color:var(--ink); line-height:1.5; overflow-wrap:anywhere; }.admin-multiline { white-space:pre-wrap; }
.admin-status-editor { display:grid; grid-template-columns:minmax(0,1fr) minmax(220px,360px); gap:16px; align-items:end; }.admin-status-editor h2,.admin-status-editor p { grid-column:1 / -1; }.admin-status-editor p { margin:-8px 0 0; color:var(--muted); }.admin-status-editor form { display:contents; }.admin-status-editor .button { align-self:end; }
.admin-login-shell { display:grid; min-height:100vh; place-items:center; padding:20px; }.admin-login-card { width:min(100%,430px); padding:32px; border:1px solid var(--line); border-top:5px solid var(--accent); border-radius:6px; background:#fff; box-shadow:0 16px 36px rgba(7,44,33,.08); }.admin-login-card .eyebrow { margin:0 0 7px; }.admin-login-card h1 { margin:0; color:var(--brand); font-size:28px; }.admin-login-card .lead { margin:12px 0 22px; }.admin-login-card form { display:grid; gap:18px; }.admin-login-card .button { width:100%; }.admin-login-error { margin:0 0 16px; padding:10px 12px; border-left:5px solid #a33a2b; background:#fff0ed; color:#a33a2b; font-size:14px; font-weight:700; }.admin-password-control { position:relative; display:block; }.admin-password-control input { padding-right:48px; }.admin-password-toggle { position:absolute; top:50%; right:6px; display:grid; width:32px; height:32px; place-items:center; padding:0; border:0; border-radius:4px; background:transparent; color:var(--brand); cursor:pointer; transform:translateY(-50%); }.admin-password-icon { width:19px; height:19px; }.admin-password-toggle:hover { background:#eaf4e5; color:#0b4938; }.admin-password-toggle:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
@media (max-width:1080px) { .admin-filters { grid-template-columns:repeat(3,minmax(0,1fr)); }.admin-filter-actions { grid-column:1 / -1; } }
@media (max-width:800px) { .admin-shell { grid-template-columns:1fr; }.admin-sidebar { position:relative; min-height:0; border-right:0; border-bottom:1px solid var(--line); }.admin-sidebar-inner { padding:12px 14px; }.admin-brand { padding:4px 2px 10px; }.admin-nav { grid-template-columns:repeat(2,minmax(0,1fr)); }.admin-logout { margin-top:10px; padding:0 2px; }.admin-main { padding:18px 14px; }.admin-topbar { padding:12px 14px; }.admin-stats-grid { grid-template-columns:1fr; }.admin-page-head,.admin-detail-head { align-items:flex-start; flex-direction:column; }.admin-filters { grid-template-columns:1fr; }.admin-filter-actions { grid-column:auto; align-items:stretch; flex-direction:column; }.admin-filter-actions .button { width:100%; }.admin-table-wrap { overflow:visible; }.admin-leads-table,.admin-leads-table tbody,.admin-leads-table tr,.admin-leads-table td { display:block; min-width:0; width:100%; }.admin-leads-table thead { display:none; }.admin-leads-table tbody { display:grid; gap:10px; padding:10px; }.admin-leads-table tr { border:1px solid var(--line); border-radius:6px; }.admin-leads-table td { display:grid; grid-template-columns:112px minmax(0,1fr); gap:10px; padding:9px 12px; border-bottom:1px solid #e7eee9; }.admin-leads-table td::before { content:attr(data-label); color:var(--muted); font-size:12px; font-weight:800; }.admin-leads-table .admin-empty { display:block; }.admin-leads-table .admin-empty::before { content:""; }.admin-detail-grid { grid-template-columns:1fr; }.admin-detail-section:last-child { grid-column:auto; }.admin-status-editor { grid-template-columns:1fr; }.admin-status-editor .button { width:100%; }.admin-pagination { flex-wrap:wrap; }.admin-pagination .button { flex:1 1 auto; text-align:center; } }
""".strip().encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "LeadScanTest/1.0"

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/admin/login":
            if self.admin_session_nonce():
                self.redirect("/admin")
                return
            self.send_bytes(admin_login_page(), "text/html; charset=utf-8")
            return
        if parsed.path == "/admin":
            session_nonce = self.require_admin_session()
            if not session_nonce:
                return
            query = parse_qs(parsed.query)
            filters = clean_admin_filters(query)
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                page = 1
            self.send_bytes(admin_lead_list_page(filters, page, session_nonce), "text/html; charset=utf-8")
            return
        if parsed.path == "/admin/export.csv":
            session_nonce = self.require_admin_session()
            if not session_nonce:
                return
            self.export_admin_leads(clean_admin_filters(parse_qs(parsed.query)))
            return
        if parsed.path.startswith("/admin/leads/"):
            session_nonce = self.require_admin_session()
            if not session_nonce:
                return
            lead_id = parsed.path.removeprefix("/admin/leads/")
            lead = get_lead(lead_id)
            if not lead:
                self.send_error_page(HTTPStatus.NOT_FOUND, "Lead not found")
                return
            updated = parse_qs(parsed.query).get("updated", [""])[0] == "1"
            self.send_bytes(admin_lead_detail_page(lead, session_nonce, updated), "text/html; charset=utf-8")
            return
        if parsed.path == "/":
            record = get_qr_code(DEFAULT_QR_CODE)
            self.send_bytes(dashboard_page(request_base_url(self), DEFAULT_QR_CODE, record), "text/html; charset=utf-8")
            return
        if parsed.path == "/assets/app.css":
            self.send_bytes(CSS, "text/css; charset=utf-8")
            return
        if parsed.path.startswith("/qr/") and parsed.path.endswith(".svg"):
            code = parsed.path.removeprefix("/qr/").removesuffix(".svg")
            record = get_qr_code(code)
            if not record or record["status"] != "active":
                self.send_error_page(HTTPStatus.NOT_FOUND, "QR code not found")
                return
            self.send_bytes(make_qr_svg(f"{request_base_url(self)}/q/{code}"), "image/svg+xml")
            return
        if parsed.path.startswith("/q/"):
            code = parsed.path.removeprefix("/q/")
            record = get_qr_code(code)
            if not record or record["status"] != "active":
                self.send_error_page(HTTPStatus.NOT_FOUND, "This QR code is inactive")
                return
            visitor_id = self.get_or_create_visitor_id()
            record_event(code, "qr_opened", visitor_id, self.client_address[0], self.headers.get("User-Agent", ""))
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", record["destination_path"])
            self.send_header("Set-Cookie", f"lead_scan_visitor={visitor_id}; Path=/; Max-Age=2592000; SameSite=Lax; Secure")
            self.end_headers()
            return
        if parsed.path.startswith("/entry/"):
            code = parsed.path.removeprefix("/entry/")
            record = get_qr_code(code)
            if not record or record["status"] != "active":
                self.send_error_page(HTTPStatus.NOT_FOUND, "This QR code is inactive")
                return
            visitor_id = self.get_or_create_visitor_id()
            record_event(code, "landing_viewed", visitor_id, self.client_address[0], self.headers.get("User-Agent", ""))
            self.send_bytes(entry_page(code, record), "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/thanks/"):
            lead_id = parsed.path.removeprefix("/thanks/")
            lead = get_lead(lead_id)
            if not lead:
                self.send_error_page(HTTPStatus.NOT_FOUND, "Enquiry not found")
                return
            self.send_bytes(thanks_page(lead), "text/html; charset=utf-8")
            return
        if parsed.path == "/internal/analytics":
            self.analytics(parsed)
            return
        if parsed.path == "/internal/leads":
            self.leads(parsed)
            return
        self.send_error_page(HTTPStatus.NOT_FOUND, "Page not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/admin/login":
            self.admin_login()
            return
        if parsed.path == "/admin/logout":
            self.admin_logout()
            return
        if parsed.path.startswith("/admin/leads/") and parsed.path.endswith("/status"):
            self.admin_update_lead_status(parsed.path)
            return
        if not parsed.path.startswith("/entry/"):
            self.send_error_page(HTTPStatus.NOT_FOUND, "Page not found")
            return

        code = parsed.path.removeprefix("/entry/")
        record = get_qr_code(code)
        if not record or record["status"] != "active":
            self.send_error_page(HTTPStatus.NOT_FOUND, "This QR code is inactive")
            return

        parsed_form = self.read_form_body()
        if parsed_form is None:
            return
        raw_values: dict[str, str | list[str]] = {
            field: parsed_form.get(field, [""])
            if field == "intended_product"
            else parsed_form.get(field, [""])[0]
            for field in FIELD_LIMITS
        }
        values, errors = validate_lead_form(raw_values)
        if errors:
            self.send_bytes(
                entry_page(code, record, values, errors),
                "text/html; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return

        lead_id = create_lead(code, values)
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/thanks/{lead_id}")
        self.end_headers()

    def read_form_body(self) -> dict[str, list[str]] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            self.send_error_page(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Unsupported form submission")
            return None
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error_page(HTTPStatus.BAD_REQUEST, "Invalid form submission")
            return None
        if content_length < 0 or content_length > MAX_FORM_BODY_BYTES:
            self.send_error_page(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Form submission is too large")
            return None
        try:
            form_data = self.rfile.read(content_length).decode("utf-8")
        except UnicodeDecodeError:
            self.send_error_page(HTTPStatus.BAD_REQUEST, "Invalid form submission")
            return None
        return parse_qs(form_data, keep_blank_values=True)

    def redirect(self, location: str, status: HTTPStatus = HTTPStatus.FOUND) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.end_headers()

    def admin_session_nonce(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        return parse_admin_session(get_cookie_value(cookie, ADMIN_SESSION_COOKIE))

    def require_admin_session(self) -> str | None:
        session_nonce = self.admin_session_nonce()
        if not session_nonce:
            self.redirect("/admin/login")
            return None
        return session_nonce

    def set_admin_session_cookie(self, token: str, max_age: int) -> None:
        cookie = (
            f"{ADMIN_SESSION_COOKIE}={token}; Path=/admin; Max-Age={max_age}; "
            "HttpOnly; SameSite=Strict"
        )
        if request_base_url(self).startswith("https://"):
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)

    def admin_login(self) -> None:
        form = self.read_form_body()
        if form is None:
            return
        if not admin_access_configured():
            self.send_bytes(
                admin_login_page("Administrator access is not configured."),
                "text/html; charset=utf-8",
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        password = form.get("password", [""])[0]
        if not hmac.compare_digest(password, admin_password()):
            self.send_bytes(
                admin_login_page("Incorrect password. Please try again."),
                "text/html; charset=utf-8",
                HTTPStatus.UNAUTHORIZED,
            )
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/admin")
        self.set_admin_session_cookie(create_admin_session(), ADMIN_SESSION_MAX_AGE_SECONDS)
        self.end_headers()

    def admin_logout(self) -> None:
        session_nonce = self.require_admin_session()
        if not session_nonce:
            return
        form = self.read_form_body()
        if form is None:
            return
        expected_token = csrf_token_for_session(session_nonce)
        supplied_token = form.get("csrf_token", [""])[0]
        if not hmac.compare_digest(supplied_token, expected_token):
            self.send_error_page(HTTPStatus.FORBIDDEN, "Invalid request")
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/admin/login")
        self.set_admin_session_cookie("", 0)
        self.end_headers()

    def admin_update_lead_status(self, path: str) -> None:
        session_nonce = self.require_admin_session()
        if not session_nonce:
            return
        lead_id = path.removeprefix("/admin/leads/").removesuffix("/status")
        if not lead_id or "/" in lead_id or not get_lead(lead_id):
            self.send_error_page(HTTPStatus.NOT_FOUND, "Lead not found")
            return
        form = self.read_form_body()
        if form is None:
            return
        supplied_token = form.get("csrf_token", [""])[0]
        if not hmac.compare_digest(supplied_token, csrf_token_for_session(session_nonce)):
            self.send_error_page(HTTPStatus.FORBIDDEN, "Invalid request")
            return
        status = form.get("status", [""])[0].strip().lower()
        if not update_lead_status(lead_id, status):
            self.send_error_page(HTTPStatus.BAD_REQUEST, "Invalid lead status")
            return
        self.redirect(f"/admin/leads/{lead_id}?updated=1", HTTPStatus.SEE_OTHER)

    def export_admin_leads(self, filters: dict[str, str]) -> None:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow((
            "Reference", "Submitted (Australia/Sydney)", "Status", "Full Name", "Phone Number",
            "Street Address", "Suburb", "State / Territory", "Postcode", "Intended Product",
            "Other Requirements", "QR Source",
        ))
        for lead in list_filtered_leads(filters):
            writer.writerow((
                lead["id"], format_sydney_datetime(lead["submitted_at"]), status_label(lead["status"]),
                lead["full_name"], lead["phone_number"], lead["street_address"], lead["suburb"],
                lead["state_territory"], lead["postcode"], lead["intended_product"],
                lead["other_requirements"], lead["qr_code"],
            ))
        body = output.getvalue().encode("utf-8-sig")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="advinsys-leads.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def analytics(self, parsed) -> None:
        expected_key = os.environ.get("ADMIN_KEY", "").strip()
        supplied_key = self.headers.get("X-Admin-Key", "")
        if not expected_key or not secrets.compare_digest(expected_key, supplied_key):
            self.send_bytes(b'{"error":"Unauthorized"}', "application/json; charset=utf-8", HTTPStatus.UNAUTHORIZED)
            return
        code = parse_qs(parsed.query).get("code", [DEFAULT_QR_CODE])[0]
        record = get_qr_code(code)
        if not record:
            self.send_bytes(b'{"error":"QR code not found"}', "application/json; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        self.send_bytes(
            json.dumps({
                "code": code,
                "name": record["name"],
                "qrOpened": event_count(code, "qr_opened"),
                "landingViewed": event_count(code, "landing_viewed"),
            }).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def leads(self, parsed) -> None:
        expected_key = os.environ.get("ADMIN_KEY", "").strip()
        supplied_key = self.headers.get("X-Admin-Key", "")
        if not expected_key or not secrets.compare_digest(expected_key, supplied_key):
            self.send_bytes(b'{"error":"Unauthorized"}', "application/json; charset=utf-8", HTTPStatus.UNAUTHORIZED)
            return
        query = parse_qs(parsed.query)
        try:
            limit = int(query.get("limit", ["50"])[0])
        except ValueError:
            limit = 50
        limit = min(max(limit, 1), 100)
        code = query.get("code", [""])[0].strip() or None
        if code and not get_qr_code(code):
            self.send_bytes(b'{"error":"QR code not found"}', "application/json; charset=utf-8", HTTPStatus.NOT_FOUND)
            return
        leads = [dict(lead) for lead in list_leads(limit, code)]
        self.send_bytes(
            json.dumps({"leads": leads, "count": len(leads)}).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def get_or_create_visitor_id(self) -> str:
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"(?:^|;\\s*)lead_scan_visitor=([^;]+)", cookie)
        return match.group(1) if match else secrets.token_urlsafe(18)

    def send_error_page(self, status: HTTPStatus, message: str) -> None:
        body = f"<main class=\"shell\"><section class=\"card result\"><p class=\"eyebrow\">QR TEST</p><h1>{html.escape(message)}</h1><p class=\"note\">Ask the event staff for an active QR code.</p></section></main>"
        self.send_bytes(html_page(message, body), "text/html; charset=utf-8", status)

    def send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Public QR redirect test service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8082)
    args = parser.parse_args()
    initialise_database()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"QR test service listening at http://{args.host}:{args.port}", flush=True)
    print("Open it through a public HTTPS tunnel before displaying the QR code to customers.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
