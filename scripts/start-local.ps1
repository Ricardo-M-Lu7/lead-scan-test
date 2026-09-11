param(
  [int]$Port = 8082
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$EnvFile = Join-Path $ProjectRoot ".env"

if (Test-Path -LiteralPath $EnvFile) {
  Get-Content -LiteralPath $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) {
      return
    }
    $separatorIndex = $line.IndexOf("=")
    if ($separatorIndex -lt 1) {
      throw "Invalid .env entry: $line"
    }
    $name = $line.Substring(0, $separatorIndex).Trim()
    $value = $line.Substring($separatorIndex + 1)
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
  }
}

$RequiredAdminSettings = @("ADMIN_PASSWORD", "ADMIN_SESSION_SECRET")
$MissingAdminSettings = @(
  $RequiredAdminSettings | Where-Object {
    [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($_, "Process"))
  }
)
if ($MissingAdminSettings.Count -gt 0) {
  throw "Set $($MissingAdminSettings -join ', ') before starting the service. Copy .env.example to .env for local configuration."
}

if (-not (Test-Path $VenvPython)) {
  python -m venv (Join-Path $ProjectRoot ".venv")
}

& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt")
& $VenvPython (Join-Path $ProjectRoot "server.py") --host 127.0.0.1 --port $Port
