<#
.SYNOPSIS
    Stops the 4 pods and both Cloudflare quick tunnels started by
    scripts/start_public_demo.ps1.
#>

$ErrorActionPreference = "SilentlyContinue"

Write-Host "Stopping cloudflared tunnels..." -ForegroundColor Cyan
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force

Write-Host "Stopping pods (uvicorn on 8000-8003, mcp_server.http_main)..." -ForegroundColor Cyan
foreach ($port in 8000, 8001, 8002) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}
# mcp-pod listens on 8003 but via the mcp SDK's own server; same port-based stop.
$mcpConns = Get-NetTCPConnection -LocalPort 8003 -State Listen -ErrorAction SilentlyContinue
foreach ($c in $mcpConns) {
    Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
}

Write-Host "Public demo stopped." -ForegroundColor Green
# Dead tunnel URLs would make health-check.yml fail; with the variables unset it skips.
Write-Host "Removing GitHub variables HEALER_WEBHOOK_URL/PUBLIC_URL..." -ForegroundColor Cyan
gh variable delete HEALER_WEBHOOK_URL 2>$null
gh variable delete PUBLIC_URL 2>$null
