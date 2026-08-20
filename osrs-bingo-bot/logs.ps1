# Pulls recent logs from the bingo bot server for quick debugging -- no
# need to manually SSH in every time something breaks.
#
# Run from PowerShell:
#   .\logs.ps1            # last 40 lines, then exits
#   .\logs.ps1 -Lines 100  # last 100 lines instead
#   .\logs.ps1 -Follow     # live-tails instead (like tailing a log file --
#                          # stays open, Ctrl+C to stop)

param(
    [int]$Lines = 40,
    [switch]$Follow
)

# ---- Server IP lives in server-config.ps1 (gitignored, not committed) ----
. "$PSScriptRoot\server-config.ps1"

$RemoteUser = "root"

if ($Follow) {
    Write-Host "==> Live-tailing bingo-bot logs on $ServerIP (Ctrl+C to stop)..." -ForegroundColor Cyan
    ssh "${RemoteUser}@${ServerIP}" "journalctl -u bingo-bot -f"
} else {
    Write-Host "==> Last $Lines lines of bingo-bot logs on $ServerIP..." -ForegroundColor Cyan
    ssh "${RemoteUser}@${ServerIP}" "journalctl -u bingo-bot -n $Lines --no-pager"
}
