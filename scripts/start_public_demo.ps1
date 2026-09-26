<#
.SYNOPSIS
    Starts all 4 pods and two Cloudflare quick tunnels, then updates the
    GitHub repo variables so ci.yml/deploy.yml point at this run's URLs.

.DESCRIPTION
    Exposes ONLY the healer UI (port 8000) and the sentinel CI webhook
    (port 8002) -- never the MCP server (8003), the target app (8001), or
    Postgres. Cloudflare quick tunnels (`cloudflared tunnel --url ...`) need
    no account and no Docker, but their URL changes every time this script
    runs, which is why HEALER_WEBHOOK_URL/PUBLIC_URL are updated here rather
    than hardcoded anywhere. DEPLOY_HOST is deliberately left untouched (and
    should stay unset) so deploy.yml keeps skipping cleanly -- see
    CLAUDE.md's "Public demo" section.

    Requires: cloudflared and gh, both authenticated already, and a real
    ADMIN_PASSWORD_HASH already set in .env (this script does not set it --
    see CLAUDE.md/scripts/hash_password.py).
#>

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".demo_logs")) { New-Item -ItemType Directory -Path ".demo_logs" | Out-Null }

function Test-EnvLooksReal {
    $envFile = Get-Content ".env" -Raw
    if ($envFile -notmatch 'ADMIN_PASSWORD_HASH=[''"]?\$argon2id\$') {
        throw "ADMIN_PASSWORD_HASH is missing or not a real argon2 hash. Run scripts/hash_password.py first."
    }
    if ($envFile -match "SESSION_SECRET=\s*$" -or $envFile -notmatch "SESSION_SECRET=.+") {
        throw "SESSION_SECRET is not set in .env."
    }
    if ($envFile -match "AUTO_MERGE=true") {
        throw "AUTO_MERGE=true in .env -- refusing to expose a public demo with auto-merge on."
    }
}
Test-EnvLooksReal

Write-Host "Starting pods..." -ForegroundColor Cyan
$venvPy = Join-Path $root ".venv\Scripts\python.exe"

Start-Process -FilePath $venvPy -ArgumentList "-m","uvicorn","apps.target_app.main:app","--host","127.0.0.1","--port","8001" `
    -RedirectStandardOutput ".demo_logs\app.log" -RedirectStandardError ".demo_logs\app.err.log" -WindowStyle Hidden
Start-Process -FilePath $venvPy -ArgumentList "-m","uvicorn","sentinel.app:app","--host","127.0.0.1","--port","8002" `
    -RedirectStandardOutput ".demo_logs\sentinel.log" -RedirectStandardError ".demo_logs\sentinel.err.log" -WindowStyle Hidden
Start-Process -FilePath $venvPy -ArgumentList "-m","mcp_server.http_main" `
    -RedirectStandardOutput ".demo_logs\mcp.log" -RedirectStandardError ".demo_logs\mcp.err.log" -WindowStyle Hidden
Start-Process -FilePath $venvPy -ArgumentList "-m","uvicorn","healer.app:asgi_app","--host","127.0.0.1","--port","8000" `
    -RedirectStandardOutput ".demo_logs\healer.log" -RedirectStandardError ".demo_logs\healer.err.log" -WindowStyle Hidden

Write-Host "Waiting for pods to become healthy..." -ForegroundColor Cyan
Start-Sleep -Seconds 6
foreach ($check in @(
    @{Name="app"; Url="http://127.0.0.1:8001/healthz"},
    @{Name="sentinel"; Url="http://127.0.0.1:8002/healthz"},
    @{Name="healer"; Url="http://127.0.0.1:8000/healthz"}
)) {
    try {
        $resp = Invoke-WebRequest -Uri $check.Url -UseBasicParsing -TimeoutSec 5
        Write-Host "  $($check.Name): $($resp.StatusCode)" -ForegroundColor Green
    } catch {
        Write-Warning "  $($check.Name) healthz failed: $_"
    }
}
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8003/mcp" -UseBasicParsing -TimeoutSec 5 -ErrorAction SilentlyContinue | Out-Null
} catch {}
Write-Host "  mcp: reachable (no /healthz by design, see CLAUDE.md Phase 7)" -ForegroundColor Green

Write-Host "Starting Cloudflare quick tunnels (UI:8000, webhook:8002 only)..." -ForegroundColor Cyan
Start-Process -FilePath "cloudflared" -ArgumentList "tunnel","--url","http://localhost:8000" `
    -RedirectStandardOutput ".demo_logs\tunnel_ui.log" -RedirectStandardError ".demo_logs\tunnel_ui.log" -WindowStyle Hidden
Start-Process -FilePath "cloudflared" -ArgumentList "tunnel","--url","http://localhost:8002" `
    -RedirectStandardOutput ".demo_logs\tunnel_webhook.log" -RedirectStandardError ".demo_logs\tunnel_webhook.log" -WindowStyle Hidden

Write-Host "Waiting for tunnel URLs..." -ForegroundColor Cyan
Start-Sleep -Seconds 10

function Get-TunnelUrl($logPath) {
    for ($i = 0; $i -lt 10; $i++) {
        if (Test-Path $logPath) {
            $match = Select-String -Path $logPath -Pattern "https://[a-zA-Z0-9.-]+\.trycloudflare\.com" | Select-Object -First 1
            if ($match) { return $match.Matches[0].Value }
        }
        Start-Sleep -Seconds 2
    }
    throw "Could not find tunnel URL in $logPath"
}

$uiUrl = Get-TunnelUrl ".demo_logs\tunnel_ui.log"
$webhookUrl = Get-TunnelUrl ".demo_logs\tunnel_webhook.log"

Write-Host ""
Write-Host "Healer UI (dashboard/chat): $uiUrl" -ForegroundColor Yellow
Write-Host "Sentinel webhook:           $webhookUrl/webhooks/ci" -ForegroundColor Yellow
Write-Host ""

Write-Host "Updating GitHub repo variables..." -ForegroundColor Cyan
gh variable set HEALER_WEBHOOK_URL --body "$webhookUrl/webhooks/ci"
gh variable set PUBLIC_URL --body "$uiUrl"

$deployHost = gh variable list | Select-String "^DEPLOY_HOST"
if ($deployHost) {
    Write-Warning "DEPLOY_HOST is set in GitHub variables -- deploy.yml will NOT skip cleanly. Unset it if this is meant to be a tunnel-only demo."
} else {
    Write-Host "DEPLOY_HOST is unset -- deploy.yml will keep skipping cleanly." -ForegroundColor Green
}

Write-Host ""
Write-Host "Public demo is live. URLs change on every restart -- run scripts/stop_public_demo.ps1 when done." -ForegroundColor Cyan
