#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script with sudo: sudo bash install-on-ubuntu.sh SOURCE_DIR" >&2
    exit 1
fi

SOURCE_DIR="${1:-}"
if [[ -z "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/server.py" || ! -f "${SOURCE_DIR}/requirements.txt" ]]; then
    echo "SOURCE_DIR must contain server.py and requirements.txt" >&2
    exit 1
fi
SOURCE_DIR="$(realpath "${SOURCE_DIR}")"

APP_DIR="/opt/lead-scan"
SERVICE_USER="leadscan"
ENV_DIR="/etc/lead-scan"
ENV_FILE="${ENV_DIR}/lead-scan.env"
BACKUP_DIR="/var/backups/lead-scan"
NGINX_SITE="/etc/nginx/sites-available/cellar"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "Installing runtime package (no distribution upgrade or reboot)."
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3.12-venv

if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o root -g root -m 0755 "${APP_DIR}"
rsync -a --delete --exclude '.venv/' --exclude 'data/' "${SOURCE_DIR}/" "${APP_DIR}/"
chown -R root:root "${APP_DIR}"
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${APP_DIR}/data"

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
    python3 -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/python" -m pip install --disable-pip-version-check -r "${APP_DIR}/requirements.txt"

install -d -o root -g root -m 0750 "${ENV_DIR}"
touch "${ENV_FILE}"
chmod 0640 "${ENV_FILE}"

upsert_env() {
    local key="$1"
    local value="$2"
    if grep -q "^${key}=" "${ENV_FILE}"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
    else
        printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
    fi
}

env_value() {
    awk -F= -v key="$1" '$1 == key { print substr($0, index($0, "=") + 1); exit }' "${ENV_FILE}"
}

upsert_env PUBLIC_BASE_URL "https://cellar.yesthing.com"
upsert_env PUBLIC_BASE_PATH "/lead-scan"
if [[ -z "$(env_value ADMIN_KEY)" || "$(env_value ADMIN_KEY)" == replace-* ]]; then
    upsert_env ADMIN_KEY "$(openssl rand -hex 32)"
fi
if [[ -z "$(env_value ADMIN_PASSWORD)" || "$(env_value ADMIN_PASSWORD)" == replace-* ]]; then
    upsert_env ADMIN_PASSWORD "$(openssl rand -hex 24)"
fi
if [[ -z "$(env_value ADMIN_SESSION_SECRET)" || "$(env_value ADMIN_SESSION_SECRET)" == replace-* ]]; then
    upsert_env ADMIN_SESSION_SECRET "$(openssl rand -hex 48)"
fi
chown root:"${SERVICE_USER}" "${ENV_FILE}"

install -d -o root -g root -m 0750 "${BACKUP_DIR}"
cat > /usr/local/sbin/lead-scan-backup <<'BACKUP_SCRIPT'
#!/usr/bin/env bash
set -Eeuo pipefail
db="/opt/lead-scan/data/scan-events.sqlite3"
out="/var/backups/lead-scan/scan-events-$(date +%Y%m%d-%H%M%S).sqlite3"
if [[ ! -f "$db" ]]; then
    exit 0
fi
python3 - "$db" "$out" <<'PY'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
PY
chmod 0640 "$out"
find /var/backups/lead-scan -type f -name 'scan-events-*.sqlite3' -mtime +30 -delete
BACKUP_SCRIPT
chmod 0750 /usr/local/sbin/lead-scan-backup

cat > /etc/systemd/system/lead-scan.service <<'SERVICE_UNIT'
[Unit]
Description=Advinsys QR lead capture service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=leadscan
Group=leadscan
WorkingDirectory=/opt/lead-scan
EnvironmentFile=/etc/lead-scan/lead-scan.env
ExecStart=/opt/lead-scan/.venv/bin/python /opt/lead-scan/server.py --host 127.0.0.1 --port 8082
Restart=on-failure
RestartSec=5
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/lead-scan/data

[Install]
WantedBy=multi-user.target
SERVICE_UNIT

cat > /etc/systemd/system/lead-scan-backup.service <<'BACKUP_UNIT'
[Unit]
Description=Backup Advinsys lead capture database

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/lead-scan-backup
BACKUP_UNIT

cat > /etc/systemd/system/lead-scan-backup.timer <<'BACKUP_TIMER'
[Unit]
Description=Daily Advinsys lead capture database backup

[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true

[Install]
WantedBy=timers.target
BACKUP_TIMER

if [[ ! -f "${NGINX_SITE}" ]]; then
    echo "Expected Nginx site file not found: ${NGINX_SITE}" >&2
    exit 1
fi
NGINX_BACKUP="${NGINX_SITE}.bak.${STAMP}"
cp -a "${NGINX_SITE}" "${NGINX_BACKUP}"
if ! grep -q 'location = /lead-scan' "${NGINX_SITE}"; then
    python3 - "${NGINX_SITE}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
marker = "    location / {\n"
block = """    location = /lead-scan {
        return 301 /lead-scan/;
    }

    location ^~ /lead-scan/ {
        proxy_pass http://127.0.0.1:8082;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Prefix /lead-scan;
    }

"""
if marker not in text:
    raise SystemExit("Nginx location marker not found")
path.write_text(text.replace(marker, block + marker, 1))
PY
fi

nginx -t
systemctl daemon-reload
systemctl enable --now lead-scan.service
curl --fail --silent --show-error http://127.0.0.1:8082/ >/dev/null
systemctl enable --now lead-scan-backup.timer
systemctl reload nginx

echo "Deployment completed."
echo "Service: $(systemctl is-active lead-scan.service)"
echo "Backup timer: $(systemctl is-active lead-scan-backup.timer)"
echo "URL: https://cellar.yesthing.com/lead-scan/"
echo "Admin password is stored in ${ENV_FILE}; retrieve it only from the server when needed."
