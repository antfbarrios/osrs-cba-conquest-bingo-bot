# Restarts the bingo bot on the server without deploying any file changes.
# Useful after editing .env, team_channels.py, team_sheets.py, or anything
# else directly on the server, or just to bounce the bot if it's acting up.
#
# Run from PowerShell:
#   .\restart.ps1

# ---- Server IP lives in server-config.ps1 (gitignored, not committed) ----
. "$PSScriptRoot\server-config.ps1"

$RemoteUser = "root"

Write-Host "==> Restarting bingo-bot on $ServerIP..." -ForegroundColor Cyan
ssh "${RemoteUser}@${ServerIP}" "systemctl restart bingo-bot"

Write-Host "==> Status:" -ForegroundColor Cyan
ssh "${RemoteUser}@${ServerIP}" "systemctl status bingo-bot --no-pager -l | head -n 12"

Write-Host "==> Done. Tail live logs with:" -ForegroundColor Green
Write-Host "    ssh ${RemoteUser}@${ServerIP} 'journalctl -u bingo-bot -f'"
