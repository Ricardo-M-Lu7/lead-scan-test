from __future__ import annotations

import argparse
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
import xlsxwriter


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "scan-events.sqlite3"
DEFAULT_QR_CODE = "onsite-test"
HOST_PATTERN = re.compile(r"^[A-Za-z0-9.-]+(?::\d{1,5})?$")
DB_LOCK = threading.RLock()
AU_STATES = ("ACT", "NSW", "NT", "QLD", "SA", "TAS", "VIC", "WA")
PRODUCT_OPTIONS = (
    ("pool_wave_maker", "Portable SWIM JET X"),
    ("pool_heat_pump", "Pool Heat Pump"),
    ("pool_pump", "Robotic Pool Cleaner"),
    ("robotic_lawn_mower", "Robotic Lawn Mower"),
    ("tennis_ball_machine", "Tennis Robot"),
)
PRODUCT_LABELS = dict(PRODUCT_OPTIONS)
PRODUCT_SEPARATOR = "; "
CUSTOMER_TYPE_OPTIONS = (
    ("business", "Business"),
    ("individual", "Individual"),
)
CUSTOMER_TYPE_LABELS = {
    **dict(CUSTOMER_TYPE_OPTIONS),
    "unknown": "Unknown (legacy)",
}
MAX_FORM_BODY_BYTES = 20_000
FIELD_LIMITS = {
    "customer_type": 20,
    "full_name": 160,
    "phone_number": 32,
    "email_address": 254,
    "street_address": 200,
    "suburb": 100,
    "state_territory": 3,
    "postcode": 4,
    "intended_product": 200,
    "other_requirements": 2_000,
}
LEAD_STATUSES = ("new", "contacted", "qualified", "closed")
LEADS_PER_PAGE = 25
TRASH_RETENTION_DAYS = 30
MAX_ADMIN_SEARCH_CHARS = 100
ADMIN_SESSION_COOKIE = "advinsys_admin_session"
ADMIN_SESSION_MAX_AGE_SECONDS = 8 * 60 * 60
SYDNEY_TIME_ZONE = ZoneInfo("Australia/Sydney")
EXCEL_EXPORT_HEADERS = (
    "Reference", "Submitted (Australia/Sydney)", "Status", "Customer Type", "Full Name", "Phone Number", "Email Address",
    "Street Address", "Suburb", "State / Territory", "Postcode", "Intended Product",
    "Other Requirements",
)
EXCEL_EXPORT_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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
                customer_type TEXT NOT NULL DEFAULT 'unknown'
                    CHECK(customer_type IN ('business', 'individual', 'unknown')),
                full_name TEXT NOT NULL,
                phone_number TEXT NOT NULL,
                email_address TEXT NOT NULL DEFAULT '',
                street_address TEXT NOT NULL,
                suburb TEXT NOT NULL,
                state_territory TEXT NOT NULL,
                postcode TEXT NOT NULL,
                intended_product TEXT NOT NULL,
                other_requirements TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'contacted', 'qualified', 'closed')),
                status_updated_at TEXT NOT NULL,
                deleted_at TEXT,
                deleted_by TEXT,
                FOREIGN KEY(qr_code) REFERENCES qr_codes(code)
            );

            CREATE TABLE IF NOT EXISTS lead_actions (
                id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('deleted', 'restored')),
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            """
        )
        lead_columns = {row["name"] for row in connection.execute("PRAGMA table_info(leads)")}
        if "email_address" not in lead_columns:
            connection.execute("ALTER TABLE leads ADD COLUMN email_address TEXT NOT NULL DEFAULT ''")
        if "customer_type" not in lead_columns:
            connection.execute(
                "ALTER TABLE leads ADD COLUMN customer_type TEXT NOT NULL DEFAULT 'unknown' "
                "CHECK(customer_type IN ('business', 'individual', 'unknown'))"
            )
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
        if "deleted_at" not in lead_columns:
            connection.execute("ALTER TABLE leads ADD COLUMN deleted_at TEXT")
        if "deleted_by" not in lead_columns:
            connection.execute("ALTER TABLE leads ADD COLUMN deleted_by TEXT")
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_leads_submitted_at ON leads(submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_leads_qr_code ON leads(qr_code);
            CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
            CREATE INDEX IF NOT EXISTS idx_leads_deleted_at ON leads(deleted_at);
            CREATE INDEX IF NOT EXISTS idx_lead_actions_lead_id ON lead_actions(lead_id);
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
                id, qr_code, customer_type, full_name, phone_number, email_address, street_address, suburb,
                state_territory, postcode, intended_product, other_requirements, submitted_at,
                status, status_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                lead_id,
                code,
                values["customer_type"],
                values["full_name"],
                values["phone_number"],
                values.get("email_address", ""),
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
            SELECT id, qr_code, customer_type, full_name, phone_number, email_address, street_address, suburb,
                   state_territory, postcode, intended_product, other_requirements, submitted_at,
                   status, status_updated_at, deleted_at, deleted_by
            FROM leads WHERE id = ? AND deleted_at IS NULL
            """,
            (lead_id,),
        ).fetchone()


def get_lead_in_trash(lead_id: str) -> sqlite3.Row | None:
    with DB_LOCK, database_connection() as connection:
        return connection.execute(
            """
            SELECT id, qr_code, customer_type, full_name, phone_number, email_address, street_address, suburb,
                   state_territory, postcode, intended_product, other_requirements, submitted_at,
                   status, status_updated_at, deleted_at, deleted_by
            FROM leads
            WHERE id = ? AND deleted_at IS NOT NULL AND deleted_at >= ?
            """,
            (lead_id, trash_cutoff_utc()),
        ).fetchone()


def list_leads(limit: int, code: str | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT id, qr_code, customer_type, full_name, phone_number, email_address, street_address, suburb,
               state_territory, postcode, intended_product, other_requirements, submitted_at,
               status, status_updated_at
        FROM leads
    """
    query += " WHERE deleted_at IS NULL"
    params: tuple[object, ...] = (limit,)
    if code:
        query += " AND qr_code = ?"
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


def trash_cutoff_utc() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS)).isoformat(timespec="seconds")


def lead_filter_sql(filters: dict[str, str], view: str = "active") -> tuple[str, list[object]]:
    conditions: list[str] = ["deleted_at IS NULL"] if view == "active" else ["deleted_at IS NOT NULL", "deleted_at >= ?"]
    params: list[object] = []
    if view == "trash":
        params.append(trash_cutoff_utc())
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


def lead_count(filters: dict[str, str], view: str = "active") -> int:
    where, params = lead_filter_sql(filters, view)
    with DB_LOCK, database_connection() as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM leads{where}", params).fetchone()[0])


def list_admin_leads(filters: dict[str, str], page: int, view: str = "active") -> list[sqlite3.Row]:
    where, params = lead_filter_sql(filters, view)
    params.extend((LEADS_PER_PAGE, (page - 1) * LEADS_PER_PAGE))
    query = f"""
        SELECT id, qr_code, customer_type, full_name, phone_number, email_address, street_address, suburb,
               state_territory, postcode, intended_product, other_requirements, submitted_at,
               status, status_updated_at
        FROM leads{where}
        ORDER BY submitted_at DESC
        LIMIT ? OFFSET ?
    """
    with DB_LOCK, database_connection() as connection:
        return connection.execute(query, params).fetchall()


def list_filtered_leads(filters: dict[str, str], view: str = "active") -> list[sqlite3.Row]:
    where, params = lead_filter_sql(filters, view)
    query = f"""
        SELECT id, qr_code, customer_type, full_name, phone_number, email_address, street_address, suburb,
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
            WHERE deleted_at IS NULL
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
            "UPDATE leads SET status = ?, status_updated_at = ? WHERE id = ? AND deleted_at IS NULL",
            (status, utc_now(), lead_id),
        )
    return result.rowcount == 1


def soft_delete_leads(lead_ids: list[str], actor: str = "admin") -> int:
    ids = [lead_id for lead_id in dict.fromkeys(lead_ids) if lead_id and "/" not in lead_id]
    if not ids:
        return 0
    now = utc_now()
    changed = 0
    with DB_LOCK, database_connection() as connection:
        for lead_id in ids:
            result = connection.execute(
                "UPDATE leads SET deleted_at = ?, deleted_by = ? WHERE id = ? AND deleted_at IS NULL",
                (now, actor, lead_id),
            )
            if result.rowcount == 1:
                connection.execute(
                    "INSERT INTO lead_actions (id, lead_id, action, actor, occurred_at) VALUES (?, ?, 'deleted', ?, ?)",
                    (secrets.token_urlsafe(12), lead_id, actor, now),
                )
                changed += 1
    return changed


def restore_lead(lead_id: str, actor: str = "admin") -> bool:
    now = utc_now()
    with DB_LOCK, database_connection() as connection:
        result = connection.execute(
            "UPDATE leads SET deleted_at = NULL, deleted_by = NULL WHERE id = ? AND deleted_at IS NOT NULL AND deleted_at >= ?",
            (lead_id, trash_cutoff_utc()),
        )
        if result.rowcount != 1:
            return False
        connection.execute(
            "INSERT INTO lead_actions (id, lead_id, action, actor, occurred_at) VALUES (?, ?, 'restored', ?, ?)",
            (secrets.token_urlsafe(12), lead_id, actor, now),
        )
    return True


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


def admin_view_query(filters: dict[str, str], view: str = "active", page: int | None = None) -> str:
    values = {key: value for key, value in filters.items() if value}
    if view == "trash":
        values["view"] = "trash"
    if page and page > 1:
        values["page"] = str(page)
    return urlencode(values)


def admin_export_filename(filters: dict[str, str], view: str = "active") -> str:
    scope = "trash" if view == "trash" else ("filtered" if any(filters.values()) else "all")
    timestamp = datetime.now(SYDNEY_TIME_ZONE).strftime("%Y%m%d-%H%M%S")
    return f"advinsys-leads-{scope}-{timestamp}.xlsx"


def excel_sydney_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone(SYDNEY_TIME_ZONE).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


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
        "customer_type": "Choose whether you are enquiring as a business or individual.",
        "full_name": "Enter your full name.",
        "phone_number": "Enter your phone number.",
        "email_address": "Enter your email address.",
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
    if values["customer_type"] and values["customer_type"] not in dict(CUSTOMER_TYPE_OPTIONS):
        errors["customer_type"] = "Choose either business or individual."

    phone_compact = re.sub(r"[\s()-]", "", values["phone_number"])
    if values["phone_number"] and not re.fullmatch(r"\+?\d{8,15}", phone_compact):
        errors["phone_number"] = "Enter a valid phone number with 8 to 15 digits."
    if values["email_address"] and not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*",
        values["email_address"],
    ):
        errors["email_address"] = "Enter a valid email address."
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


PUBLIC_BASE_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$")


def configured_public_base_path() -> str:
    value = os.environ.get("PUBLIC_BASE_PATH", "").strip()
    if not value or value == "/":
        return ""
    if not value.startswith("/"):
        value = "/" + value
    value = value.rstrip("/")
    return value if PUBLIC_BASE_PATH_PATTERN.fullmatch(value) else ""


def app_path(path: str) -> str:
    """Prefix an application-relative path with the configured public mount path."""
    if not path.startswith("/"):
        path = "/" + path
    base_path = configured_public_base_path()
    if not base_path or path == base_path or path.startswith(base_path + "/"):
        return path
    if path == "/":
        return base_path + "/"
    return base_path + path


def strip_public_base_path(path: str) -> str:
    base_path = configured_public_base_path()
    if not base_path:
        return path
    if path == base_path or path == base_path + "/":
        return "/"
    if path.startswith(base_path + "/"):
        return path[len(base_path):] or "/"
    return path


def request_base_url(handler: BaseHTTPRequestHandler) -> str:
    configured = configured_public_base_url()
    if configured:
        return configured + configured_public_base_path()

    host = handler.headers.get("Host", "")
    if not HOST_PATTERN.fullmatch(host):
        return "http://127.0.0.1:8082"

    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    scheme = "https" if forwarded_proto == "https" else "http"
    return f"{scheme}://{host}{configured_public_base_path()}"


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
  <link rel=\"stylesheet\" href=\"{app_path('/assets/app.css')}\">
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
    <img class=\"qr\" src=\"{app_path(f'/qr/{code}.svg')}\" alt=\"QR code for the on-site redirect test\">
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
    required: bool = True,
    wide: bool = False,
) -> str:
    value = html.escape(values.get(field, ""), quote=True)
    invalid = ' aria-invalid="true" aria-describedby="error-' + field + '"' if field in errors else ""
    autocomplete_attr = f' autocomplete="{html.escape(autocomplete, quote=True)}"' if autocomplete else ""
    optional_label = ' <span class="optional">(Optional)</span>' if not required else ""
    return f"""
    <label class="field-card{' field-card-wide' if wide else ''}">
      <span class="field-label">{html.escape(label)}{optional_label}</span>
      <input class="field-input" type="{input_type}" name="{field}" value="{value}"{autocomplete_attr}
        placeholder="{html.escape(placeholder, quote=True)}"{' required' if required else ''}{invalid}>
      {field_error(field, errors)}
    </label>"""


def customer_type_field(values: dict[str, str], errors: dict[str, str]) -> str:
    selected_type = values.get("customer_type", "")
    invalid = ' aria-invalid="true" aria-describedby="error-customer_type"' if "customer_type" in errors else ""
    options = "".join(
        f'''<label class="customer-type-option"><input type="radio" name="customer_type" value="{html.escape(value, quote=True)}"{' checked' if value == selected_type else ''}{' required' if value == CUSTOMER_TYPE_OPTIONS[0][0] else ''}{invalid}><span>{html.escape(label)}</span></label>'''
        for value, label in CUSTOMER_TYPE_OPTIONS
    )
    return f"""
    <fieldset class="customer-type-field">
      <legend class="sr-only">Customer type</legend>
      <div class="customer-type-options">{options}</div>
      {field_error("customer_type", errors)}
    </fieldset>"""


def textarea_field(label: str, field: str, values: dict[str, str], errors: dict[str, str]) -> str:
    invalid = ' aria-invalid="true" aria-describedby="error-' + field + '"' if field in errors else ""
    value = html.escape(values.get(field, ""))
    return f"""
    <label class="field-card field-card-wide">
      <span class="field-label">{html.escape(label)} <span class="optional">(Optional)</span></span>
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
      <details class="product-picker" data-product-picker aria-label="Products of interest" aria-required="true"{invalid}>
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
    form_title = "Please correct the highlighted fields." if errors else "Please complete the form below"
    body = f"""
<div class="form-shell">
  <aside class="sidebar" aria-label="Enquiry progress">
    <div class="sidebar-inner">
      <div class="side-card">
        <p class="side-kicker">ADVINSYS</p>
        <h2 class="side-title">Customer Enquiry</h2>
        <p class="side-copy">Let us know your details and product interests.</p>
        <div class="progress" aria-label="Step 2 of 3">
          <div class="progress-meta"><span>Progress</span><span>2 of 3</span></div>
          <div class="progress-bar"><span style="width: 66%"></span></div>
        </div>
      </div>
      <nav class="nav" aria-label="Enquiry steps">
        <div class="nav-item is-done"><span class="nav-num">1</span><span><strong class="nav-title">Scan complete.</strong><small class="nav-sub">You're now connected to Advinsys.</small></span></div>
        <div class="nav-item is-current"><span class="nav-num">2</span><span><strong class="nav-title">Your Details.</strong><small class="nav-sub">us how we can help.</small></span></div>
        <div class="nav-item"><span class="nav-num">3</span><span><strong class="nav-title">Confirmation.</strong><small class="nav-sub">We will process your enquiry.</small></span></div>
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
        <form class="panel" method="post" action="{app_path(f'/entry/{html.escape(code, quote=True)}')}" novalidate>
          <div class="section-head"><span class="section-num">01</span><h2>Customer type</h2></div>
          {customer_type_field(values, errors)}
          <div class="section-head section-head-spaced"><span class="section-num">02</span><h2>Contact details</h2></div>
          <div class="grid grid-2">
            {text_field("Full Name", "full_name", values, errors, autocomplete="name", placeholder="Your full name")}
            {text_field("Phone Number", "phone_number", values, errors, autocomplete="tel", input_type="tel", placeholder="e.g. 04XX XXX XXX")}
            {text_field("Email Address", "email_address", values, errors, autocomplete="email", input_type="email", placeholder="e.g. name@example.com", wide=True)}
          </div>
          <div class="section-head section-head-spaced"><span class="section-num">03</span><h2>Address</h2></div>
          <div class="grid grid-2">
            {text_field("Street Address", "street_address", values, errors, autocomplete="street-address", placeholder="Street number and name")}
            {text_field("Suburb", "suburb", values, errors, autocomplete="address-level2", placeholder="Your suburb")}
            {state_field(values, errors)}
            {text_field("Postcode", "postcode", values, errors, autocomplete="postal-code", input_type="text", placeholder="4-digit postcode")}
          </div>
          <div class="section-head section-head-spaced"><span class="section-num">04</span><h2>PRODUCTS OF INTEREST</h2></div>
          <div class="grid grid-2">
            {product_field(values, errors)}
            {textarea_field("Additional Comments", "other_requirements", values, errors)}
          </div>
          <div class="footer-actions"><div class="footer-actions-inner"><span class="form-note">Fields marked as required must be completed.</span><button class="button button-primary" type="submit">Submit Enquiry</button></div></div>
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
    trash_class = " is-active" if active == "trash" else ""
    body = f"""
<div class="admin-shell">
  <aside class="admin-sidebar" aria-label="Lead management navigation">
    <div class="admin-sidebar-inner">
      <a class="admin-brand" href="{app_path('/admin')}"><span>ADVINSYS</span><strong>Lead Management</strong></a>
      <nav class="admin-nav" aria-label="Primary navigation">
        <a class="admin-nav-item{all_leads_class}" href="{app_path('/admin')}">All Leads</a>
        <a class="admin-nav-item{trash_class}" href="{app_path('/admin?view=trash')}">Recycle bin</a>
      </nav>
      <form class="admin-logout" method="post" action="{app_path('/admin/logout')}"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}"><button class="admin-text-button" type="submit">Sign out</button></form>
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
    <form method="post" action="{app_path('/admin/login')}">
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


def admin_delete_modal() -> str:
    return """
<div class="admin-modal" id="delete-confirm-modal" hidden>
  <div class="admin-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-confirm-title" aria-describedby="delete-confirm-message">
    <h2 id="delete-confirm-title">Confirm deletion</h2>
    <p id="delete-confirm-message">Are you sure you want to delete the selected lead?</p>
    <div class="admin-modal-actions">
      <button class="button admin-button-secondary" type="button" data-delete-cancel>Cancel</button>
      <button class="button admin-button-danger" type="button" data-delete-confirm>Confirm delete</button>
    </div>
  </div>
</div>
<script>
  (() => {
    const modal = document.getElementById("delete-confirm-modal");
    if (!modal) return;
    const message = modal.querySelector("#delete-confirm-message");
    const cancel = modal.querySelector("[data-delete-cancel]");
    const confirm = modal.querySelector("[data-delete-confirm]");
    let targetForm = null;
    let lastFocused = null;
    const close = () => {
      modal.hidden = true;
      document.body.classList.remove("admin-modal-open");
      if (lastFocused) lastFocused.focus();
      targetForm = null;
    };
    const open = (trigger) => {
      targetForm = document.getElementById(trigger.dataset.deleteForm);
      if (!targetForm) return;
      lastFocused = trigger;
      const count = Number(trigger.dataset.deleteCount || "1");
      message.textContent = count > 1
        ? `Are you sure you want to delete these ${count} leads?`
        : "Are you sure you want to delete this lead?";
      modal.hidden = false;
      document.body.classList.add("admin-modal-open");
      cancel.focus();
    };
    document.querySelectorAll("[data-delete-trigger]").forEach((trigger) => {
      trigger.addEventListener("click", () => open(trigger));
    });
    confirm.addEventListener("click", () => {
      if (targetForm) targetForm.requestSubmit();
    });
    cancel.addEventListener("click", close);
    modal.addEventListener("click", (event) => {
      if (event.target === modal) close();
    });
    document.addEventListener("keydown", (event) => {
      if (!modal.hidden && event.key === "Escape") close();
    });
  })();
</script>"""


def admin_lead_list_page(filters: dict[str, str], page: int, session_nonce: str, view: str = "active", deleted_count: int = 0, restored: bool = False) -> bytes:
    is_trash = view == "trash"
    total = lead_count(filters, view)
    last_page = max(1, (total + LEADS_PER_PAGE - 1) // LEADS_PER_PAGE)
    page = min(max(page, 1), last_page)
    leads = list_admin_leads(filters, page, view)
    csrf_token = csrf_token_for_session(session_nonce)
    select_all = "" if is_trash else '<input type="checkbox" id="select-all-leads" aria-label="Select all leads">'
    selection_cell = "" if is_trash else '<td data-label="Select"><input type="checkbox" name="lead_ids" value="{lead_id}"></td>'
    action_cell = "" if not is_trash else '<td data-label="Actions"><form method="post" action="' + app_path('/admin/leads/{lead_id}/restore') + '"><input type="hidden" name="csrf_token" value="' + html.escape(csrf_token, quote=True) + '"><button class="admin-text-button" type="submit">Restore lead</button></form></td>'
    rows = "".join(
        f"""
        <tr>
          {selection_cell.format(lead_id=html.escape(lead['id'], quote=True))}
          <td data-label="Submitted">{html.escape(format_sydney_datetime(lead['submitted_at']))}</td>
          <td data-label="Customer type">{html.escape(CUSTOMER_TYPE_LABELS.get(lead['customer_type'], "Unknown (legacy)"))}</td>
          <td data-label="Name"><a class="admin-lead-link" href="{app_path(f"/admin/leads/{html.escape(lead['id'], quote=True)}{'?view=trash' if is_trash else ''}")}">{html.escape(lead['full_name'])}</a></td>
          <td data-label="Phone">{html.escape(lead['phone_number'])}</td>
          <td data-label="Location">{html.escape(lead['suburb'])}, {html.escape(lead['state_territory'])} {html.escape(lead['postcode'])}</td>
          <td data-label="Product">{html.escape(lead['intended_product'])}</td>
          <td data-label="Status"><span class="lead-status lead-status-{html.escape(lead['status'], quote=True)}">{html.escape(status_label(lead['status']))}</span></td>
          {action_cell.format(lead_id=html.escape(lead['id'], quote=True))}
        </tr>"""
        for lead in leads
    )
    if not rows:
        rows = '<tr><td class="admin-empty" colspan="8">No leads match these filters.</td></tr>'
    previous_href = app_path(f"/admin?{admin_view_query(filters, view, page - 1)}") if page > 1 else ""
    next_href = app_path(f"/admin?{admin_view_query(filters, view, page + 1)}") if page < last_page else ""
    previous = f'<a class="button admin-button-secondary" href="{html.escape(previous_href, quote=True)}">Previous</a>' if previous_href else '<span class="button admin-button-disabled">Previous</span>'
    next_link = f'<a class="button admin-button-secondary" href="{html.escape(next_href, quote=True)}">Next</a>' if next_href else '<span class="button admin-button-disabled">Next</span>'
    export_query = admin_view_query(filters, view)
    export_href = app_path("/admin/export.xlsx" + (f"?{export_query}" if export_query else ""))
    page_title = "Recycle bin" if is_trash else "Lead Management"
    page_description = "Restore recently deleted enquiries." if is_trash else "Review and follow up customer enquiries."
    notice = ""
    if deleted_count:
        notice = f'<p class="admin-success" role="status">{deleted_count} lead{"s" if deleted_count != 1 else ""} moved to the recycle bin.</p>'
    elif restored:
        notice = '<p class="admin-success" role="status">Lead restored.</p>'
    table_headers = (f"<th>{select_all}</th>" if not is_trash else "") + "<th>Submitted</th><th>Customer type</th><th>Name</th><th>Phone</th><th>Location</th><th>Product</th><th>Status</th>" + ("<th>Actions</th>" if is_trash else "")
    bulk_action = "" if is_trash else f'''<form class="admin-bulk-form" id="bulk-delete-form" method="post" action="{app_path('/admin/leads/batch-delete')}"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">'''
    bulk_action_end = "" if is_trash else "</form>"
    content = f"""
<section class="admin-page-head"><div><p class="eyebrow">ADVINSYS</p><h1>{page_title}</h1><p class="lead">{page_description}</p></div><a class="button button-primary" href="{html.escape(export_href, quote=True)}">Export Excel</a></section>
{notice}
{'' if is_trash else f'<section class="admin-stats-grid">{admin_stats_markup(admin_summary_counts())}</section>'}
<form class="admin-filters" method="get" action="{app_path('/admin')}">
  <input type="hidden" name="view" value="{html.escape(view, quote=True)}">
  <label><span>Search</span><input type="search" name="search" value="{html.escape(filters['search'], quote=True)}" placeholder="Name, phone, product, or requirement"></label>
  <label><span>Status</span><select name="status">{status_options(filters['status'], include_all=True)}</select></label>
  <label><span>State / Territory</span><select name="state">{state_options(filters['state'])}</select></label>
  <label><span>From</span><input type="date" name="date_from" value="{html.escape(filters['date_from'], quote=True)}"></label>
  <label><span>To</span><input type="date" name="date_to" value="{html.escape(filters['date_to'], quote=True)}"></label>
  <div class="admin-filter-actions"><button class="button button-primary" type="submit">Apply filters</button><a class="admin-clear-link" href="{app_path('/admin' + ('?view=trash' if is_trash else ''))}">Clear filters</a></div>
</form>
<section class="admin-table-section" aria-label="Leads">
  <div class="admin-table-meta"><strong>{total} lead{'s' if total != 1 else ''}</strong><span>Page {page} of {last_page}</span></div>
  {bulk_action}
  {'' if is_trash else '<div class="admin-bulk-actions"><span class="admin-selection-count" data-selection-count>0 selected</span><button class="button admin-button-danger" id="delete-selected-leads" type="button" data-delete-trigger data-delete-form="bulk-delete-form" data-delete-count="0" disabled>Delete selected</button></div>'}
  <div class="admin-table-wrap"><table class="admin-leads-table"><thead><tr>{table_headers}</tr></thead><tbody>{rows}</tbody></table></div>
  {bulk_action_end}
  <nav class="admin-pagination" aria-label="Lead pages">{previous}<span>Page {page} of {last_page}</span>{next_link}</nav>
</section>
<script>
  (() => {{
    const selectAll = document.getElementById("select-all-leads");
    if (!selectAll) return;
    const deleteButton = document.getElementById("delete-selected-leads");
    const selectionCount = document.querySelector("[data-selection-count]");
    const boxes = Array.from(document.querySelectorAll('input[name="lead_ids"]'));
    const updateSelection = () => {{
      const selected = boxes.filter((box) => box.checked).length;
      selectAll.checked = boxes.length > 0 && selected === boxes.length;
      if (selectionCount) selectionCount.textContent = `${{selected}} selected`;
      if (deleteButton) {{
        deleteButton.disabled = selected === 0;
        deleteButton.dataset.deleteCount = String(selected);
      }}
    }};
    selectAll.addEventListener("change", () => {{ boxes.forEach((box) => {{ box.checked = selectAll.checked; }}); updateSelection(); }});
    boxes.forEach((box) => box.addEventListener("change", updateSelection));
    updateSelection();
  }})();
</script>"""
    content += admin_delete_modal() if not is_trash else ""
    return admin_layout(f"{page_title} | Advinsys", content, session_nonce, "trash" if is_trash else "leads")


def admin_lead_detail_page(lead: sqlite3.Row, session_nonce: str, updated: bool = False, deleted: bool = False, restored: bool = False) -> bytes:
    update_message = '<p class="admin-success" role="status">Lead status updated.</p>' if updated else ""
    if deleted:
        update_message = '<p class="admin-success" role="status">Lead moved to the recycle bin.</p>'
    if restored:
        update_message = '<p class="admin-success" role="status">Lead restored.</p>'
    csrf_token = csrf_token_for_session(session_nonce)
    address_lines = "<br>".join(
        html.escape(value)
        for value in (
            lead["street_address"],
            f"{lead['suburb']}, {lead['state_territory']} {lead['postcode']}",
        )
    )
    requirements = html.escape(lead["other_requirements"]) if lead["other_requirements"] else "Not provided"
    deleted_at = lead["deleted_at"] or ""
    delete_action = (
        f'<section class="admin-detail-section admin-danger-zone"><h2>Recycle bin</h2><p>Move this lead to the recycle bin. It can be restored within {TRASH_RETENTION_DAYS} days.</p><form id="single-delete-form" method="post" action="{app_path(f"/admin/leads/{html.escape(lead['id'], quote=True)}/delete")}"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}"><button class="button admin-button-danger" type="button" data-delete-trigger data-delete-form="single-delete-form" data-delete-count="1">Delete lead</button></form></section>'
        if not deleted else
        f'<section class="admin-detail-section admin-danger-zone"><h2>Recycle bin</h2><p>Deleted {html.escape(format_sydney_datetime(deleted_at))}. You can restore this lead within {TRASH_RETENTION_DAYS} days.</p><form method="post" action="{app_path(f"/admin/leads/{html.escape(lead['id'], quote=True)}/restore")}"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}"><button class="button button-primary" type="submit">Restore lead</button></form></section>'
    )
    status_editor = "" if deleted else f'<section class="admin-detail-section admin-status-editor"><h2>Lead status</h2><p>Update the status after each follow-up.</p><form method="post" action="{app_path(f"/admin/leads/{html.escape(lead['id'], quote=True)}/status")}"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}"><label><span>Status</span><select name="status">{status_options(lead["status"])}</select></label><button class="button button-primary" type="submit">Update status</button></form></section>'
    back_href = app_path("/admin" + ("?view=trash" if deleted else ""))
    content = f"""
<section class="admin-detail-head"><div><a class="button admin-button-secondary admin-back-link" href="{back_href}">Back to {'recycle bin' if deleted else 'all leads'}</a><p class="eyebrow">LEAD DETAIL</p><h1>{html.escape(lead['full_name'])}</h1><p class="lead">Submitted {html.escape(format_sydney_datetime(lead['submitted_at']))}</p></div><span class="lead-status lead-status-{html.escape(lead['status'], quote=True)}">{html.escape(status_label(lead['status']))}</span></section>
{update_message}
<div class="admin-detail-grid">
  <section class="admin-detail-section"><h2>Contact details</h2><dl><div><dt>Customer type</dt><dd>{html.escape(CUSTOMER_TYPE_LABELS.get(lead['customer_type'], "Unknown (legacy)"))}</dd></div><div><dt>Full name</dt><dd>{html.escape(lead['full_name'])}</dd></div><div><dt>Phone number</dt><dd>{html.escape(lead['phone_number'])}</dd></div><div><dt>Email address</dt><dd>{html.escape(lead['email_address']) if lead['email_address'] else "Not provided"}</dd></div><div><dt>Address</dt><dd>{address_lines}</dd></div></dl></section>
  <section class="admin-detail-section"><h2>Enquiry</h2><dl><div><dt>Intended product</dt><dd>{html.escape(lead['intended_product'])}</dd></div><div><dt>Other requirements</dt><dd class="admin-multiline">{requirements}</dd></div><div><dt>Reference</dt><dd>{html.escape(lead['id'])}</dd></div></dl></section>
  {status_editor}
  {delete_action}
</div>"""
    if not deleted:
        content += admin_delete_modal()
    return admin_layout(f"{lead['full_name']} | Lead Management", content, session_nonce, "trash" if deleted else "leads")


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
.sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
.customer-type-field { min-width:0; margin:0; padding:0; border:0; }
.customer-type-options { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); overflow:hidden; border:1px solid var(--line); border-bottom:3px solid var(--brand); border-radius:6px; background:#fff; }
.customer-type-option { display:flex; align-items:center; gap:10px; min-height:52px; padding:12px 14px; color:var(--brand); font-size:14px; font-weight:800; cursor:pointer; }
.customer-type-option + .customer-type-option { border-left:1px solid var(--line); }
.customer-type-option:hover { background:#eef7f2; }.customer-type-option:has(input:checked) { background:#eaf4e5; }
.customer-type-option input { width:18px; height:18px; margin:0; accent-color:var(--brand); }.customer-type-option input:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
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
@media (max-width:620px) { .app { padding:14px; }.topbar,.page-footer { padding-left:14px; padding-right:14px; }.page-banner h1 { font-size:28px; }.grid-2,.customer-type-options { grid-template-columns:1fr; }.field-card-wide { grid-column:auto; }.customer-type-option + .customer-type-option { border-top:1px solid var(--line); border-left:0; }.nav { grid-template-columns:1fr; }.footer-actions-inner { align-items:stretch; flex-direction:column; }.button { width:100%; }.page-footer { flex-direction:column; }.panel { padding:14px; } }
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
.admin-bulk-form { margin:0; }
.admin-bulk-actions { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:12px 18px; border-bottom:1px solid var(--line); background:#fbfdfb; }
.admin-selection-count { color:var(--muted); font-size:13px; font-weight:700; }
.admin-button-danger { background:#a33a2b; color:#fff; }.admin-button-danger:hover { background:#842d22; }
.admin-danger-zone { border-color:#e6b8b0; }.admin-danger-zone p { color:var(--muted); }.admin-danger-zone form { margin:0; }
.admin-modal[hidden] { display:none; }
.admin-modal { position:fixed; z-index:20; inset:0; display:grid; place-items:center; padding:20px; background:rgba(10,31,24,.52); }
.admin-modal-dialog { width:min(100%,440px); padding:26px; border:1px solid var(--line); border-top:5px solid #a33a2b; border-radius:8px; background:#fff; box-shadow:0 24px 60px rgba(7,44,33,.24); }
.admin-modal-dialog h2 { margin:0 0 10px; color:var(--brand); font-size:22px; }
.admin-modal-dialog p { margin:0; color:var(--muted); line-height:1.5; }
.admin-modal-actions { display:flex; justify-content:flex-end; gap:10px; margin-top:24px; }
.admin-modal-actions .button { min-width:116px; }
.admin-modal-open { overflow:hidden; }
.admin-table-meta,.admin-pagination { display:flex; align-items:center; justify-content:space-between; gap:14px; padding:15px 18px; color:var(--muted); font-size:13px; }
.admin-table-meta { border-bottom:1px solid var(--line); }.admin-table-meta strong { color:var(--brand); }
.admin-table-wrap { max-height:560px; overflow-x:auto; overflow-y:auto; }
.admin-leads-table { width:100%; border-collapse:collapse; min-width:920px; text-align:left; }
.admin-leads-table th { position:sticky; top:0; z-index:1; padding:12px 14px; border-bottom:1px solid var(--line); background:#fff; color:var(--muted); font-size:12px; font-weight:800; text-transform:uppercase; }
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
@media (max-width:800px) { .admin-shell { grid-template-columns:1fr; }.admin-sidebar { position:relative; min-height:0; border-right:0; border-bottom:1px solid var(--line); }.admin-sidebar-inner { padding:12px 14px; }.admin-brand { padding:4px 2px 10px; }.admin-nav { grid-template-columns:repeat(2,minmax(0,1fr)); }.admin-logout { margin-top:10px; padding:0 2px; }.admin-main { padding:18px 14px; }.admin-topbar { padding:12px 14px; }.admin-stats-grid { grid-template-columns:1fr; }.admin-page-head,.admin-detail-head { align-items:flex-start; flex-direction:column; }.admin-filters { grid-template-columns:1fr; }.admin-filter-actions { grid-column:auto; align-items:stretch; flex-direction:column; }.admin-filter-actions .button { width:100%; }.admin-table-wrap { max-height:none; overflow:visible; }.admin-leads-table,.admin-leads-table tbody,.admin-leads-table tr,.admin-leads-table td { display:block; min-width:0; width:100%; }.admin-leads-table thead { display:none; }.admin-leads-table tbody { display:grid; gap:10px; padding:10px; }.admin-leads-table tr { border:1px solid var(--line); border-radius:6px; }.admin-leads-table td { display:grid; grid-template-columns:112px minmax(0,1fr); gap:10px; padding:9px 12px; border-bottom:1px solid #e7eee9; }.admin-leads-table td::before { content:attr(data-label); color:var(--muted); font-size:12px; font-weight:800; }.admin-leads-table .admin-empty { display:block; }.admin-leads-table .admin-empty::before { content:""; }.admin-detail-grid { grid-template-columns:1fr; }.admin-detail-section:last-child { grid-column:auto; }.admin-status-editor { grid-template-columns:1fr; }.admin-status-editor .button { width:100%; }.admin-pagination { flex-wrap:wrap; }.admin-pagination .button { flex:1 1 auto; text-align:center; }.admin-bulk-actions { align-items:stretch; flex-direction:column; }.admin-bulk-actions .button { width:100%; }.admin-modal-actions { flex-direction:column; }.admin-modal-actions .button { width:100%; } }
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
        route_path = strip_public_base_path(parsed.path)
        if route_path == "/admin/login":
            if self.admin_session_nonce():
                self.redirect("/admin")
                return
            self.send_bytes(admin_login_page(), "text/html; charset=utf-8")
            return
        if route_path == "/admin":
            session_nonce = self.require_admin_session()
            if not session_nonce:
                return
            query = parse_qs(parsed.query)
            filters = clean_admin_filters(query)
            view = query.get("view", ["active"])[0].strip().lower()
            if view not in ("active", "trash"):
                view = "active"
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                page = 1
            try:
                deleted_count = max(0, int(query.get("deleted", ["0"])[0]))
            except ValueError:
                deleted_count = 0
            restored = query.get("restored", [""])[0] == "1"
            self.send_bytes(admin_lead_list_page(filters, page, session_nonce, view, deleted_count, restored), "text/html; charset=utf-8")
            return
        if route_path == "/admin/export.xlsx":
            session_nonce = self.require_admin_session()
            if not session_nonce:
                return
            query = parse_qs(parsed.query)
            view = query.get("view", ["active"])[0].strip().lower()
            if view not in ("active", "trash"):
                view = "active"
            self.export_admin_leads_xlsx(clean_admin_filters(query), view)
            return
        if route_path.startswith("/admin/leads/"):
            session_nonce = self.require_admin_session()
            if not session_nonce:
                return
            lead_id = route_path.removeprefix("/admin/leads/")
            query = parse_qs(parsed.query)
            view = query.get("view", ["active"])[0].strip().lower()
            if view not in ("active", "trash"):
                view = "active"
            lead = get_lead_in_trash(lead_id) if view == "trash" else get_lead(lead_id)
            if not lead:
                self.send_error_page(HTTPStatus.NOT_FOUND, "Lead not found")
                return
            updated = query.get("updated", [""])[0] == "1"
            deleted = query.get("deleted", [""])[0] == "1"
            restored = query.get("restored", [""])[0] == "1"
            self.send_bytes(admin_lead_detail_page(lead, session_nonce, updated, view == "trash", restored), "text/html; charset=utf-8")
            return
        if route_path == "/":
            record = get_qr_code(DEFAULT_QR_CODE)
            self.send_bytes(dashboard_page(request_base_url(self), DEFAULT_QR_CODE, record), "text/html; charset=utf-8")
            return
        if route_path == "/assets/app.css":
            self.send_bytes(CSS, "text/css; charset=utf-8")
            return
        if route_path.startswith("/qr/") and route_path.endswith(".svg"):
            code = route_path.removeprefix("/qr/").removesuffix(".svg")
            record = get_qr_code(code)
            if not record or record["status"] != "active":
                self.send_error_page(HTTPStatus.NOT_FOUND, "QR code not found")
                return
            self.send_bytes(make_qr_svg(f"{request_base_url(self)}/q/{code}"), "image/svg+xml")
            return
        if route_path.startswith("/q/"):
            code = route_path.removeprefix("/q/")
            record = get_qr_code(code)
            if not record or record["status"] != "active":
                self.send_error_page(HTTPStatus.NOT_FOUND, "This QR code is inactive")
                return
            visitor_id = self.get_or_create_visitor_id()
            record_event(code, "qr_opened", visitor_id, self.client_address[0], self.headers.get("User-Agent", ""))
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", app_path(record["destination_path"]))
            self.send_header(f"Set-Cookie", f"lead_scan_visitor={visitor_id}; Path={configured_public_base_path() or '/'}; Max-Age=2592000; SameSite=Lax; Secure")
            self.end_headers()
            return
        if route_path.startswith("/entry/"):
            code = route_path.removeprefix("/entry/")
            record = get_qr_code(code)
            if not record or record["status"] != "active":
                self.send_error_page(HTTPStatus.NOT_FOUND, "This QR code is inactive")
                return
            visitor_id = self.get_or_create_visitor_id()
            record_event(code, "landing_viewed", visitor_id, self.client_address[0], self.headers.get("User-Agent", ""))
            self.send_bytes(entry_page(code, record), "text/html; charset=utf-8")
            return
        if route_path.startswith("/thanks/"):
            lead_id = route_path.removeprefix("/thanks/")
            lead = get_lead(lead_id)
            if not lead:
                self.send_error_page(HTTPStatus.NOT_FOUND, "Enquiry not found")
                return
            self.send_bytes(thanks_page(lead), "text/html; charset=utf-8")
            return
        if route_path == "/internal/analytics":
            self.analytics(parsed)
            return
        if route_path == "/internal/leads":
            self.leads(parsed)
            return
        self.send_error_page(HTTPStatus.NOT_FOUND, "Page not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        route_path = strip_public_base_path(parsed.path)
        if route_path == "/admin/login":
            self.admin_login()
            return
        if route_path == "/admin/logout":
            self.admin_logout()
            return
        if route_path == "/admin/leads/batch-delete":
            self.admin_batch_delete()
            return
        if route_path.startswith("/admin/leads/") and route_path.endswith("/delete"):
            self.admin_delete_lead(route_path)
            return
        if route_path.startswith("/admin/leads/") and route_path.endswith("/restore"):
            self.admin_restore_lead(route_path)
            return
        if route_path.startswith("/admin/leads/") and route_path.endswith("/status"):
            self.admin_update_lead_status(route_path)
            return
        if not route_path.startswith("/entry/"):
            self.send_error_page(HTTPStatus.NOT_FOUND, "Page not found")
            return

        code = route_path.removeprefix("/entry/")
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
        self.send_header("Location", app_path(f"/thanks/{lead_id}"))
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
        self.send_header("Location", app_path(location))
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
            f"{ADMIN_SESSION_COOKIE}={token}; Path={app_path('/admin')}; Max-Age={max_age}; "
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
        self.send_header("Location", app_path("/admin"))
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
        self.send_header("Location", app_path("/admin/login"))
        self.set_admin_session_cookie("", 0)
        self.end_headers()

    def validate_admin_csrf(self, session_nonce: str, form: dict[str, list[str]]) -> bool:
        supplied_token = form.get("csrf_token", [""])[0]
        if not hmac.compare_digest(supplied_token, csrf_token_for_session(session_nonce)):
            self.send_error_page(HTTPStatus.FORBIDDEN, "Invalid request")
            return False
        return True

    def admin_batch_delete(self) -> None:
        session_nonce = self.require_admin_session()
        if not session_nonce:
            return
        form = self.read_form_body()
        if form is None or not self.validate_admin_csrf(session_nonce, form):
            return
        count = soft_delete_leads(form.get("lead_ids", []))
        self.redirect(f"/admin?deleted={count}", HTTPStatus.SEE_OTHER)

    def admin_delete_lead(self, path: str) -> None:
        session_nonce = self.require_admin_session()
        if not session_nonce:
            return
        lead_id = path.removeprefix("/admin/leads/").removesuffix("/delete")
        if not lead_id or "/" in lead_id or not get_lead(lead_id):
            self.send_error_page(HTTPStatus.NOT_FOUND, "Lead not found")
            return
        form = self.read_form_body()
        if form is None or not self.validate_admin_csrf(session_nonce, form):
            return
        soft_delete_leads([lead_id])
        self.redirect("/admin?deleted=1", HTTPStatus.SEE_OTHER)

    def admin_restore_lead(self, path: str) -> None:
        session_nonce = self.require_admin_session()
        if not session_nonce:
            return
        lead_id = path.removeprefix("/admin/leads/").removesuffix("/restore")
        if not lead_id or "/" in lead_id:
            self.send_error_page(HTTPStatus.NOT_FOUND, "Lead not found")
            return
        form = self.read_form_body()
        if form is None or not self.validate_admin_csrf(session_nonce, form):
            return
        if not restore_lead(lead_id):
            self.send_error_page(HTTPStatus.NOT_FOUND, "Lead not found in the recycle bin")
            return
        self.redirect("/admin?view=trash&restored=1", HTTPStatus.SEE_OTHER)

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
        if not self.validate_admin_csrf(session_nonce, form):
            return
        status = form.get("status", [""])[0].strip().lower()
        if not update_lead_status(lead_id, status):
            self.send_error_page(HTTPStatus.BAD_REQUEST, "Invalid lead status")
            return
        self.redirect(f"/admin/leads/{lead_id}?updated=1", HTTPStatus.SEE_OTHER)

    def export_admin_leads_xlsx(self, filters: dict[str, str], view: str = "active") -> None:
        leads = list_filtered_leads(filters, view)
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(
            output,
            {
                "in_memory": True,
                "strings_to_formulas": False,
                "strings_to_urls": False,
            },
        )
        worksheet = workbook.add_worksheet("Leads")
        worksheet.hide_gridlines(0)
        worksheet.freeze_panes(1, 0)
        worksheet.set_zoom(90)

        header_format = workbook.add_format({
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#F79646",
            "align": "center",
            "valign": "vcenter",
            "border": 1,
            "border_color": "#000000",
        })
        text_format = workbook.add_format({"valign": "top", "num_format": "@", "bg_color": "#FFFFFF", "border": 1, "border_color": "#000000"})
        multiline_format = workbook.add_format({"valign": "top", "text_wrap": True, "num_format": "@", "bg_color": "#FFFFFF", "border": 1, "border_color": "#000000"})
        date_format = workbook.add_format({"valign": "top", "num_format": "dd mmm yyyy, hh:mm AM/PM", "bg_color": "#FFFFFF", "border": 1, "border_color": "#000000"})
        worksheet.set_row(0, 24)
        column_widths = (20, 27, 14, 18, 22, 18, 32, 28, 18, 18, 12, 28, 48)
        for column, width in enumerate(column_widths):
            default_format = multiline_format if column == len(EXCEL_EXPORT_HEADERS) - 1 else text_format
            worksheet.set_column(column, column, width, default_format)
        for column, header in enumerate(EXCEL_EXPORT_HEADERS):
            worksheet.write_string(0, column, header, header_format)

        for row_index, lead in enumerate(leads, start=1):
            submitted_at = excel_sydney_datetime(lead["submitted_at"])
            if submitted_at is None:
                worksheet.write_string(row_index, 1, lead["submitted_at"], text_format)
            else:
                worksheet.write_datetime(row_index, 1, submitted_at, date_format)
            values = (
                lead["id"], status_label(lead["status"]), CUSTOMER_TYPE_LABELS.get(lead["customer_type"], "Unknown (legacy)"), lead["full_name"], lead["phone_number"], lead["email_address"],
                lead["street_address"], lead["suburb"], lead["state_territory"], lead["postcode"],
                lead["intended_product"], lead["other_requirements"],
            )
            for column, value in enumerate(values):
                target_column = column if column == 0 else column + 1
                cell_format = multiline_format if target_column == len(EXCEL_EXPORT_HEADERS) - 1 else text_format
                worksheet.write_string(row_index, target_column, value or "", cell_format)

        worksheet.autofilter(0, 0, len(leads), len(EXCEL_EXPORT_HEADERS) - 1)
        workbook.set_properties({"title": "Advinsys lead export", "author": "Advinsys"})
        workbook.close()
        body = output.getvalue()
        filename = admin_export_filename(filters, view)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", EXCEL_EXPORT_CONTENT_TYPE)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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
