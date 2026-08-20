# Deploy script for the OSRS Bingo Bot.
#
# Run this from PowerShell, from inside your project folder (C:\Dev\osrs-bingo-bot):
#   .\deploy.ps1
#
# What it does:
#   1. Copies every .py file in this folder, plus requirements.txt, to the
#      server (NOT .env, NOT bingo.db, NOT the service account .json, NOT
#      venv/__pycache__ -- those are excluded explicitly below).
#   2. Recursively copies every subfolder too (e.g. assets/), except known
#      local-only folders like venv/__pycache__/.git.
#   3. Reinstalls dependencies on the server in case requirements.txt changed.
#   4. Restarts the bingo-bot systemd service.
#   5. Shows you its status so you can confirm it came back up cleanly.
#
# .env, bingo.db, and your service account key are intentionally never
# touched by this script -- those are server-specific and shouldn't be
# overwritten by whatever's sitting in your local folder.
#
# NOTE: this auto-discovers every .py file AND every subfolder, so a new
# file or folder (like assets/ was) gets picked up automatically next time
# you run this -- no need to edit this script when new files/folders get
# added, except to extend the exclusion list below if you ever add a
# folder that should NOT be deployed.

# ---- Server IP lives in server-config.ps1 (gitignored, not committed) ----
# First time only: copy server-config.ps1.example to server-config.ps1 and
# fill in your real IP.
. "$PSScriptRoot\server-config.ps1"

$RemoteUser = "root"
$RemotePath = "/root/osrs-bingo-bot"
$Target = "${RemoteUser}@${ServerIP}:${RemotePath}/"

$ExcludedFolders = @("venv", "__pycache__", ".git")

$FilesToSync = @(Get-ChildItem -Path . -Filter *.py | Select-Object -ExpandProperty Name)
$FilesToSync += "requirements.txt"

$FoldersToSync = @(Get-ChildItem -Path . -Directory | Where-Object { $ExcludedFolders -notcontains $_.Name } | Select-Object -ExpandProperty Name)

Write-Host "==> Files found to sync: $($FilesToSync -join ', ')" -ForegroundColor DarkGray
Write-Host "==> Folders found to sync: $(if ($FoldersToSync) { $FoldersToSync -join ', ' } else { '(none)' })" -ForegroundColor DarkGray
Write-Host "==> Copying code files to $ServerIP..." -ForegroundColor Cyan

$FailedFiles = @()
foreach ($file in $FilesToSync) {
    if (Test-Path $file) {
        scp $file "$Target"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "   !! FAILED to copy $file (scp exit code $LASTEXITCODE)" -ForegroundColor Red
            $FailedFiles += $file
        }
    } else {
        Write-Host "   (skipping $file -- not found locally)" -ForegroundColor DarkGray
    }
}

if ($FoldersToSync) {
    Write-Host "==> Copying folders to $ServerIP..." -ForegroundColor Cyan
    foreach ($folder in $FoldersToSync) {
        scp -r $folder "$Target"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "   !! FAILED to copy folder $folder (scp exit code $LASTEXITCODE)" -ForegroundColor Red
            $FailedFiles += "$folder/ (folder)"
        }
    }
}

if ($FailedFiles.Count -gt 0) {
    Write-Host "==> WARNING: these did NOT copy successfully: $($FailedFiles -join ', ')" -ForegroundColor Red
    Write-Host "==> Fix the issue above and re-run before trusting the restart below." -ForegroundColor Red
}

Write-Host "==> Reinstalling dependencies and restarting the bot..." -ForegroundColor Cyan
ssh "${RemoteUser}@${ServerIP}" "cd $RemotePath && source venv/bin/activate && pip install -r requirements.txt -q && systemctl restart bingo-bot"

Write-Host "==> Checking status..." -ForegroundColor Cyan
ssh "${RemoteUser}@${ServerIP}" "systemctl status bingo-bot --no-pager -l | head -n 12"

Write-Host "==> Confirming all expected .py files are present on the server..." -ForegroundColor Cyan
ssh "${RemoteUser}@${ServerIP}" "ls $RemotePath/*.py"

Write-Host "==> Done. Tail live logs with:" -ForegroundColor Green
Write-Host "    ssh ${RemoteUser}@${ServerIP} 'journalctl -u bingo-bot -f'"