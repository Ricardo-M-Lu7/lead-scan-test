# Advinsys QR Lead Capture

This project provides the first Advinsys customer journey: scan a QR code, record an anonymous scan event, open an English customer enquiry form, and store the submitted lead in SQLite.

The form collects a required `Customer Type` (`Business` or `Individual`), `Full Name`, `Phone Number`, `Street Address`, `Suburb`, `State / Territory`, `Postcode`, one or more `Intended Product` selections, and optional `Other Requirements`. Product choices are `Pool Wave Maker`, `Pool Heat Pump`, `Pool Pump`, `Pool Salt Chlorinator`, `Robotic Lawn Mower`, and `Tennis Ball Machine`. It is designed for Australian address and phone-number formats.

## Run locally

```powershell
Set-Location D:\project\lead-scan-test
.\scripts\start-local.ps1
```

The local service listens at `http://127.0.0.1:8082`. Do not show this address to a phone on another network.

## Temporary public HTTPS testing

Install `cloudflared` as a local tool, then leave the service running and start a second terminal:

```powershell
.\tools\cloudflared.exe tunnel --url http://127.0.0.1:8082
```

Cloudflare prints a random `https://*.trycloudflare.com` URL. Open that public URL on the computer or event tablet. The displayed QR code dynamically encodes that same public address, so a phone can scan it over mobile data or any Wi-Fi.

The temporary URL changes whenever the tunnel process restarts. It is for testing only.

## Stable public test domain

For a domain such as `https://lead-test.example.com`, set `PUBLIC_BASE_URL` before starting the service and configure a named tunnel or other HTTPS hosting to forward that hostname to `http://127.0.0.1:8082`.

When the service is mounted below an existing site, set `PUBLIC_BASE_PATH` to the path without a trailing slash. For example, `PUBLIC_BASE_URL=https://cellar.yesthing.com` and `PUBLIC_BASE_PATH=/lead-scan` publish the service at `https://cellar.yesthing.com/lead-scan/`.

```powershell
$env:PUBLIC_BASE_URL = "https://lead-test.example.com"
$env:PUBLIC_BASE_PATH = ""
.\scripts\start-local.ps1
```

Use a secret key to view aggregate scan counts. Do not place this key in the QR code, webpage, or source control.

```powershell
$headers = @{ "X-Admin-Key" = $env:ADMIN_KEY }
Invoke-RestMethod -Headers $headers https://lead-test.example.com/internal/analytics?code=onsite-test
```

## Test checklist

1. Open the public HTTPS URL on a computer or tablet.
2. Scan the displayed QR code using iPhone Camera, Android Camera, and the intended in-app scanner.
3. Complete the customer enquiry form and confirm the phone reaches the thank-you page.
4. Confirm the QR target begins with the public HTTPS hostname, not `localhost`.
5. Check aggregate counts and saved leads through the protected endpoints.

## Read submitted leads

Use the same `ADMIN_KEY` header to return the latest 50 leads. The optional `code` query parameter filters by QR code and `limit` accepts 1 to 100.

```powershell
$headers = @{ "X-Admin-Key" = $env:ADMIN_KEY }
Invoke-RestMethod -Headers $headers https://lead-test.example.com/internal/leads?code=onsite-test
```

## Lead Management

The English Lead Management page is available at `/admin`. It provides password-protected lead search, filtering, status updates, detail views, pagination, Excel export, soft deletion, batch deletion, and a recycle bin. Exports use filenames such as `advinsys-leads-all-20260911-154230.xlsx` or `advinsys-leads-filtered-20260911-154230.xlsx`.

Set an administrator password and a separate session-signing secret before starting the service. Keep both values out of source control.

```powershell
$env:ADMIN_PASSWORD = "choose-a-strong-administrator-password"
$env:ADMIN_SESSION_SECRET = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
.\scripts\start-local.ps1
```

Open `http://127.0.0.1:8082/admin` locally, or append `/admin` to the stable public HTTPS domain. The page uses the statuses `New`, `Contacted`, `Qualified`, and `Closed`.

Select one or more leads from the active list and choose `Delete selected`, or delete an individual lead from its detail page. A centered in-page confirmation dialog appears before deletion. Deletion is soft: active lists, counts, exports, the protected leads endpoint, and public confirmation pages exclude deleted leads. Deleted leads remain recoverable in the recycle bin for 30 days and can be restored during that period. This project does not expose a permanent-delete action; records older than the 30-day recycle-bin window remain stored but are no longer shown or recoverable through the admin UI.

## Privacy boundaries

- QR codes contain only an opaque test code.
- Events store an anonymous visitor ID, browser type, timestamp, and a one-way hash of the client IP.
- Submitted leads contain the customer information entered in the form and are only returned through the `ADMIN_KEY`-protected endpoint.
- This project does not yet provide a privacy notice, file upload, or messaging integration. Add the formal privacy content before public launch.
