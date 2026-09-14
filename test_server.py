import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

import server


def xlsx_shared_strings(body: bytes) -> list[str]:
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(text.text or "" for text in item.iter(f"{namespace}t"))
        for item in root.findall(f"{namespace}si")
    ]


def xlsx_worksheet_xml(body: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return archive.read("xl/worksheets/sheet1.xml").decode("utf-8")


def xlsx_styles_xml(body: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return archive.read("xl/styles.xml").decode("utf-8")


class ScanRedirectTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_data_dir = server.DATA_DIR
        self.original_db_path = server.DB_PATH
        self.original_admin_key = os.environ.get("ADMIN_KEY")
        self.original_admin_password = os.environ.get("ADMIN_PASSWORD")
        self.original_admin_session_secret = os.environ.get("ADMIN_SESSION_SECRET")
        self.original_public_base_url = os.environ.get("PUBLIC_BASE_URL")
        self.original_public_base_path = os.environ.get("PUBLIC_BASE_PATH")
        server.DATA_DIR = Path(self.tempdir.name)
        server.DB_PATH = server.DATA_DIR / "scan-events.sqlite3"
        os.environ["ADMIN_KEY"] = "test-admin-key"
        os.environ["ADMIN_PASSWORD"] = "test-admin-password"
        os.environ["ADMIN_SESSION_SECRET"] = "test-session-secret"
        os.environ.pop("PUBLIC_BASE_URL", None)
        os.environ.pop("PUBLIC_BASE_PATH", None)
        server.initialise_database()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.DATA_DIR = self.original_data_dir
        server.DB_PATH = self.original_db_path
        if self.original_admin_key is None:
            os.environ.pop("ADMIN_KEY", None)
        else:
            os.environ["ADMIN_KEY"] = self.original_admin_key
        if self.original_admin_password is None:
            os.environ.pop("ADMIN_PASSWORD", None)
        else:
            os.environ["ADMIN_PASSWORD"] = self.original_admin_password
        if self.original_admin_session_secret is None:
            os.environ.pop("ADMIN_SESSION_SECRET", None)
        else:
            os.environ["ADMIN_SESSION_SECRET"] = self.original_admin_session_secret
        if self.original_public_base_url is None:
            os.environ.pop("PUBLIC_BASE_URL", None)
        else:
            os.environ["PUBLIC_BASE_URL"] = self.original_public_base_url
        if self.original_public_base_path is None:
            os.environ.pop("PUBLIC_BASE_PATH", None)
        else:
            os.environ["PUBLIC_BASE_PATH"] = self.original_public_base_path
        self.tempdir.cleanup()

    def request(self, path, headers=None, method="GET", body=None):
        connection = HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_dashboard_generates_svg_for_the_current_host(self):
        status, _headers, body = self.request("/", {"Host": "public-test.example.com"})
        self.assertEqual(status, 200)
        self.assertIn(b"Scan this code with a phone", body)

        status, _headers, body = self.request("/qr/onsite-test.svg", {"Host": "public-test.example.com"})
        self.assertEqual(status, 200)
        self.assertIn(b"<svg", body)

    def test_configured_base_path_routes_and_generates_prefixed_urls(self):
        os.environ["PUBLIC_BASE_URL"] = "https://cellar.yesthing.com"
        os.environ["PUBLIC_BASE_PATH"] = "/lead-scan"

        status, _headers, body = self.request("/lead-scan/", {"Host": "cellar.yesthing.com"})
        self.assertEqual(status, 200)
        self.assertIn(b'href="/lead-scan/assets/app.css"', body)
        self.assertIn(b'src="/lead-scan/qr/onsite-test.svg"', body)
        self.assertIn(b"https://cellar.yesthing.com/lead-scan/q/onsite-test", body)

        status, headers, body = self.request("/lead-scan/q/onsite-test", {"Host": "cellar.yesthing.com"})
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/lead-scan/entry/onsite-test")
        self.assertIn("Path=/lead-scan;", headers["Set-Cookie"])

        status, _headers, body = self.request("/lead-scan/entry/onsite-test", {"Host": "cellar.yesthing.com"})
        self.assertEqual(status, 200)
        self.assertIn(b'action="/lead-scan/entry/onsite-test"', body)

        status, _headers, body = self.request("/lead-scan/admin/login", {"Host": "cellar.yesthing.com"})
        self.assertEqual(status, 200)
        self.assertIn(b'action="/lead-scan/admin/login"', body)
        login_body = urlencode({"password": "test-admin-password"}).encode("utf-8")
        status, headers, _body = self.request(
            "/lead-scan/admin/login",
            {"Host": "cellar.yesthing.com", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
            body=login_body,
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/lead-scan/admin")
        self.assertIn("Path=/lead-scan/admin;", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _headers, body = self.request(
            "/lead-scan/admin",
            {"Host": "cellar.yesthing.com", "Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertIn(b'href="/lead-scan/admin/export.xlsx"', body)

    def test_entry_page_renders_product_multiselect(self):
        status, _headers, body = self.request("/entry/onsite-test")
        self.assertEqual(status, 200)
        self.assertIn(b"Customer type", body)
        self.assertIn(b'name="customer_type" value="business"', body)
        self.assertIn(b'name="customer_type" value="individual"', body)
        self.assertIn(b'data-product-picker', body)
        self.assertIn(b'type="checkbox"', body)
        for _code, label in server.PRODUCT_OPTIONS:
            self.assertIn(label.encode("utf-8"), body)

    def test_scan_redirect_records_anonymous_events(self):
        status, headers, _body = self.request("/q/onsite-test")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/entry/onsite-test")
        self.assertIn("lead_scan_visitor=", headers["Set-Cookie"])

        status, _headers, body = self.request("/entry/onsite-test")
        self.assertEqual(status, 200)
        self.assertIn(b"How can we help?", body)
        self.assertIn(b"State / Territory", body)
        self.assertIn(b'<option value="" disabled selected>Select state or territory</option>', body)
        self.assertEqual(server.event_count("onsite-test", "qr_opened"), 1)
        self.assertEqual(server.event_count("onsite-test", "landing_viewed"), 1)

    def test_customer_enquiry_submission_persists_and_is_visible_to_admin(self):
        form = urlencode({
            "customer_type": "business",
            "full_name": "Taylor Smith",
            "phone_number": "+61 412 345 678",
            "street_address": "12 Collins Street",
            "suburb": "Melbourne",
            "state_territory": "VIC",
            "postcode": "3000",
            "intended_product": ["pool_heat_pump", "pool_pump", "pool_pump"],
            "other_requirements": "Please call in the afternoon.",
        }, doseq=True).encode("utf-8")
        status, headers, _body = self.request(
            "/entry/onsite-test",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            form,
        )
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/thanks/LD-"))

        status, _headers, body = self.request(headers["Location"])
        self.assertEqual(status, 200)
        self.assertIn(b"Thank you, Taylor Smith.", body)
        self.assertIn(b"Pool Heat Pump; Pool Pump", body)

        status, _headers, body = self.request(
            "/internal/leads?code=onsite-test",
            {"X-Admin-Key": "test-admin-key"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["leads"][0]["full_name"], "Taylor Smith")
        self.assertEqual(payload["leads"][0]["customer_type"], "business")
        self.assertEqual(payload["leads"][0]["postcode"], "3000")
        self.assertEqual(payload["leads"][0]["intended_product"], "Pool Heat Pump; Pool Pump")

    def test_customer_enquiry_requires_valid_australian_address_fields(self):
        form = urlencode({
            "customer_type": "individual",
            "full_name": "Taylor Smith",
            "phone_number": "not-a-phone-number",
            "street_address": "12 Collins Street",
            "suburb": "Melbourne",
            "state_territory": "Victoria",
            "postcode": "300",
            "intended_product": "pool_heat_pump",
        }).encode("utf-8")
        status, _headers, body = self.request(
            "/entry/onsite-test",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            form,
        )
        self.assertEqual(status, 400)
        self.assertIn(b"Enter a valid phone number", body)
        self.assertIn(b"Select an Australian state or territory", body)
        self.assertIn(b"Enter a 4-digit Australian postcode", body)

    def test_customer_enquiry_requires_product_selection_from_allowlist(self):
        fields = {
            "customer_type": "business",
            "full_name": "Taylor Smith",
            "phone_number": "+61 412 345 678",
            "street_address": "12 Collins Street",
            "suburb": "Melbourne",
            "state_territory": "VIC",
            "postcode": "3000",
            "other_requirements": "",
        }
        status, _headers, body = self.request(
            "/entry/onsite-test",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            urlencode(fields).encode("utf-8"),
        )
        self.assertEqual(status, 400)
        self.assertIn(b"Describe the product you are interested in", body)

        fields["intended_product"] = "not-a-product"
        status, _headers, body = self.request(
            "/entry/onsite-test",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            urlencode(fields).encode("utf-8"),
        )
        self.assertEqual(status, 400)
        self.assertIn(b"Select products from the list", body)

    def test_customer_enquiry_requires_a_valid_customer_type(self):
        fields = {
            "full_name": "Taylor Smith",
            "phone_number": "+61 412 345 678",
            "street_address": "12 Collins Street",
            "suburb": "Melbourne",
            "state_territory": "VIC",
            "postcode": "3000",
            "intended_product": "pool_heat_pump",
        }
        status, _headers, body = self.request(
            "/entry/onsite-test",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            urlencode(fields).encode("utf-8"),
        )
        self.assertEqual(status, 400)
        self.assertIn(b"Choose whether you are enquiring as a business or individual", body)

        fields["customer_type"] = "unknown"
        status, _headers, body = self.request(
            "/entry/onsite-test",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            urlencode(fields).encode("utf-8"),
        )
        self.assertEqual(status, 400)
        self.assertIn(b"Choose either business or individual", body)

    def test_database_migration_marks_existing_leads_as_legacy_customer_type(self):
        with sqlite3.connect(server.DB_PATH) as connection:
            connection.executescript(
                """
                DROP TABLE leads;
                CREATE TABLE leads (
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
                    FOREIGN KEY(qr_code) REFERENCES qr_codes(code)
                );
                """
            )
            connection.execute(
                """
                INSERT INTO leads (
                    id, qr_code, full_name, phone_number, street_address, suburb,
                    state_territory, postcode, intended_product, other_requirements, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "LD-LEGACY", "onsite-test", "Existing Lead", "+61 412 345 678",
                    "12 Collins Street", "Melbourne", "VIC", "3000", "Pool Heat Pump", "",
                    "2026-09-01T00:00:00+00:00",
                ),
            )

        connection.close()
        server.initialise_database()
        lead = server.get_lead("LD-LEGACY")
        self.assertIsNotNone(lead)
        self.assertEqual(lead["customer_type"], "unknown")
        self.assertEqual(lead["status"], "new")

    def test_analytics_requires_a_secret_key(self):
        status, _headers, _body = self.request("/internal/analytics")
        self.assertEqual(status, 401)

        status, _headers, body = self.request(
            "/internal/analytics",
            {"X-Admin-Key": "test-admin-key"},
        )
        self.assertEqual(status, 200)
        self.assertIn(b'"qrOpened": 0', body)

    def submit_sample_lead(self):
        form = urlencode({
            "customer_type": "individual",
            "full_name": "Taylor Smith",
            "phone_number": "+61 412 345 678",
            "street_address": "12 Collins Street",
            "suburb": "Melbourne",
            "state_territory": "VIC",
            "postcode": "3000",
            "intended_product": ["pool_heat_pump", "pool_pump"],
            "other_requirements": "Please call in the afternoon.",
        }, doseq=True).encode("utf-8")
        status, headers, _body = self.request(
            "/entry/onsite-test",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            form,
        )
        self.assertEqual(status, 303)
        return headers["Location"].removeprefix("/thanks/")

    def admin_login(self):
        form = urlencode({"password": "test-admin-password"}).encode("utf-8")
        status, headers, _body = self.request(
            "/admin/login",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            form,
        )
        self.assertEqual(status, 303)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        token = cookie.split("=", 1)[1]
        nonce = server.parse_admin_session(token)
        self.assertIsNotNone(nonce)
        return cookie, server.csrf_token_for_session(nonce)

    def test_admin_requires_login_and_supports_lead_workflow(self):
        self.submit_sample_lead()

        status, _headers, body = self.request("/admin/login")
        self.assertEqual(status, 200)
        self.assertIn(b'id="admin-password-toggle"', body)
        self.assertIn(b'admin-password-icon', body)
        self.assertIn(b"Show password", body)

        status, headers, _body = self.request("/admin")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/admin/login")

        wrong_form = urlencode({"password": "wrong"}).encode("utf-8")
        status, _headers, body = self.request(
            "/admin/login",
            {"Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            wrong_form,
        )
        self.assertEqual(status, 401)
        self.assertIn(b"Incorrect password", body)

        cookie, csrf = self.admin_login()
        status, _headers, body = self.request("/admin", {"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b"Lead Management", body)
        self.assertIn(b"Taylor Smith", body)
        self.assertIn(b"Customer type", body)
        self.assertIn(b"Individual", body)
        self.assertIn(b">New<", body)
        self.assertIn(b'Export Excel', body)
        self.assertIn(b'/admin/export.xlsx', body)
        self.assertIn(b'id="delete-confirm-modal"', body)
        self.assertIn(b'data-selection-count', body)
        self.assertNotIn(b'onsubmit="return confirm(', body)

        status, _headers, body = self.request(
            "/admin?search=heat+pump&status=new&state=VIC",
            {"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertIn(b"Taylor Smith", body)

        lead_id = server.list_leads(1)[0]["id"]
        status, _headers, body = self.request(f"/admin/leads/{lead_id}", {"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b"Contact details", body)
        self.assertIn(b"Customer type", body)
        self.assertIn(b"Individual", body)
        self.assertNotIn(b"QR source", body)
        self.assertIn(b'id="delete-confirm-modal"', body)
        self.assertIn(b'data-delete-trigger', body)
        self.assertNotIn(b'onsubmit="return confirm(', body)
        csrf_match = re.search(rb'name="csrf_token" value="([^"]+)"', body)
        self.assertIsNotNone(csrf_match)
        self.assertEqual(csrf_match.group(1).decode(), csrf)

        invalid_update = urlencode({"csrf_token": "invalid", "status": "contacted"}).encode("utf-8")
        status, _headers, _body = self.request(
            f"/admin/leads/{lead_id}/status",
            {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            invalid_update,
        )
        self.assertEqual(status, 403)

        update_form = urlencode({"csrf_token": csrf, "status": "contacted"}).encode("utf-8")
        status, headers, _body = self.request(
            f"/admin/leads/{lead_id}/status",
            {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            update_form,
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"/admin/leads/{lead_id}?updated=1")

        status, _headers, body = self.request(f"/admin/leads/{lead_id}?updated=1", {"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b"Lead status updated", body)
        self.assertIn(b">Contacted<", body)

        status, headers, body = self.request("/admin/export.xlsx", {"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(
            headers["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertRegex(
            headers["Content-Disposition"],
            r'^attachment; filename="advinsys-leads-all-\d{8}-\d{6}\.xlsx"$',
        )
        self.assertEqual(body[:2], b"PK")
        strings = xlsx_shared_strings(body)
        self.assertIn("Reference", strings)
        self.assertIn("Other Requirements", strings)
        self.assertIn("Taylor Smith", strings)
        self.assertIn("Individual", strings)
        self.assertIn("Contacted", strings)
        self.assertNotIn("QR Source", strings)
        self.assertNotIn("onsite-test", strings)
        self.assertIn('autoFilter ref="A1:L2"', xlsx_worksheet_xml(body))
        styles = xlsx_styles_xml(body)
        self.assertIn('rgb="FFFFFFFF"', styles)
        self.assertIn('rgb="FF000000"', styles)

        status, headers, body = self.request(
            "/admin/export.xlsx?search=Taylor&status=contacted&state=VIC",
            {"Cookie": cookie},
        )
        self.assertEqual(status, 200)
        self.assertRegex(
            headers["Content-Disposition"],
            r'^attachment; filename="advinsys-leads-filtered-\d{8}-\d{6}\.xlsx"$',
        )
        self.assertEqual(body[:2], b"PK")
        self.assertIn("Taylor Smith", xlsx_shared_strings(body))

        status, _headers, _body = self.request("/admin/export.csv", {"Cookie": cookie})
        self.assertEqual(status, 404)

        logout_form = urlencode({"csrf_token": csrf}).encode("utf-8")
        status, headers, _body = self.request(
            "/admin/logout",
            {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            logout_form,
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin/login")
        self.assertIn("Max-Age=0", headers["Set-Cookie"])

    def test_admin_can_batch_soft_delete_and_restore_leads(self):
        first_id = self.submit_sample_lead()
        second_id = self.submit_sample_lead()
        cookie, csrf = self.admin_login()

        status, _headers, body = self.request("/admin", {"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b'name="lead_ids"', body)
        self.assertIn(b"Recycle bin", body)

        delete_form = urlencode({"csrf_token": csrf, "lead_ids": [first_id, second_id]}, doseq=True).encode("utf-8")
        status, headers, _body = self.request(
            "/admin/leads/batch-delete",
            {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            delete_form,
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin?deleted=2")
        self.assertEqual(server.list_leads(10), [])

        status, _headers, body = self.request("/admin?view=trash", {"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn(b"Taylor Smith", body)
        self.assertIn(b"Restore lead", body)

        status, _headers, body = self.request(f"/thanks/{first_id}")
        self.assertEqual(status, 404)

        restore_form = urlencode({"csrf_token": csrf}).encode("utf-8")
        status, headers, _body = self.request(
            f"/admin/leads/{first_id}/restore",
            {"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"},
            "POST",
            restore_form,
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/admin?view=trash&restored=1")
        self.assertIsNotNone(server.get_lead(first_id))
        self.assertIsNone(server.get_lead(second_id))

        connection = sqlite3.connect(server.DB_PATH)
        connection.execute(
            "UPDATE leads SET deleted_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00+00:00", first_id),
        )
        connection.commit()
        connection.close()
        self.assertFalse(server.restore_lead(first_id))


if __name__ == "__main__":
    unittest.main()
