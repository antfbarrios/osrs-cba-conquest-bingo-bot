# Pulls the live bingo.db from the server down to your local machine, so
# your local copy matches what's actually happening in Discord/the sheets.
#
# This is intentionally ONE-WAY: server -> local, never local -> server.
# The server's database is the real, live data; your local copy is only
# useful for inspecting/backing up/running report.py against, so pushing
# a local copy up would risk overwriting real submission history with a
# stale one. deploy.ps1 explicitly excludes bingo.db for this same reason.
#
# Run from PowerShell, from inside your project folder:
#   .\sync-db.ps1

# ---- Server IP lives in server-config.ps1 (gitignored, not committed) ----
. "$PSScriptRoot\server-config.ps1"

$RemoteUser = "root"
$RemotePath = "/root/osrs-bingo-bot/bingo.db"

if (Test-Path ".\bingo.db") {
    $BackupName = "bingo.db.bak.$(Get-Date -Format 'yyyy-MM-dd_HHmm')"
    Write-Host "==> Backing up your current local bingo.db to $BackupName" -ForegroundColor DarkGray
    Copy-Item ".\bingo.db" ".\$BackupName"
}

Write-Host "==> Pulling the live database from $ServerIP..." -ForegroundColor Cyan
scp "${RemoteUser}@${ServerIP}:${RemotePath}" ".\bingo.db"

if ($LASTEXITCODE -eq 0) {
    Write-Host "==> Done. Your local bingo.db now matches the server." -ForegroundColor Green
    Write-Host "    (a backup of your old local copy is sitting next to it, if you had one)"
} else {
    Write-Host "==> FAILED to pull the database (scp exit code $LASTEXITCODE)." -ForegroundColor Red
    Write-Host "    Your local bingo.db was NOT changed." -ForegroundColor Red
}
