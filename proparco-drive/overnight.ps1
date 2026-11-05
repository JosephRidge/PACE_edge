# overnight.ps1 - unattended overnight capture of S:\Proparco (read-only) into the search database.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File D:\proparco-drive\overnight.ps1
#   (optional: -StopAt "06:30"  -Batch 3000  -Root "S:\Proparco")
#
# Repeats until the stop time or until nothing is left:
#   1. extract the next batch of new/changed files (newest first)
#   2. embed them into server.db on the stick (documents only; emails stay in proparco.db)
#   3. publish a copy of server.db to the server folder (copied under a temporary name,
#      then renamed, so readers never see a half-written file)
# Keeps the laptop awake while running (no setting is changed). Everything is logged.
# Safe to stop at any time (Ctrl+C or closing the window): the next run continues where it stopped.

param(
    [string]$Root   = "S:\Proparco",
    [string]$StopAt = "07:00",
    [int]$Batch     = 1500,
    [string]$PublishDir = "S:\Proparco\AgentServerVectorDB",
    [string]$WorkDir    = "C:\ProparcoBuild",
    [switch]$NoPublish                       # build only; upload to the server later by hand
)

Set-Location $PSScriptRoot
# All server work (extracted text, working server.db, logs) goes to the laptop's own disk,
# not the flash disk. Emails are not touched: they stay in the stick's proparco-data.
New-Item -ItemType Directory -Force $WorkDir | Out-Null
$env:PROPARCO_DATA_DIR = $WorkDir
$excludes = @("--exclude", "07. Management", "--exclude", "99. Old", "--exclude", ".pst",
              "--exclude", "AgentServerVectorDB")

# Keep the system awake for as long as this window runs (ES_CONTINUOUS | ES_SYSTEM_REQUIRED).
Add-Type -Namespace Win32 -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
'@
[void][Win32.Power]::SetThreadExecutionState([uint32]"0x80000001")

$stop = (Get-Date).Date.Add([TimeSpan]::Parse($StopAt))
if ($stop -le (Get-Date)) { $stop = $stop.AddDays(1) }
$logDir = Join-Path $WorkDir "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ("overnight-{0:yyyyMMdd-HHmm}.log" -f (Get-Date))

$localDb = Join-Path $WorkDir "db\server.db"

function Publish {
    if ($NoPublish -or -not (Test-Path $localDb)) { return }
    try {
        New-Item -ItemType Directory -Force $PublishDir -ErrorAction Stop | Out-Null
        $tmp = Join-Path $PublishDir "server.db.tmp"
        Copy-Item $localDb $tmp -Force -ErrorAction Stop
        Move-Item $tmp (Join-Path $PublishDir "server.db") -Force -ErrorAction Stop
        $mb = (Get-Item (Join-Path $PublishDir "server.db")).Length / 1MB
        Say ("Published server.db to {0} ({1:N0} MB)" -f $PublishDir, $mb)
    } catch {
        Say "WARNING: could not publish to $PublishDir ($($_.Exception.Message)). The local copy is kept: $localDb"
    }
}

function Say($text) { $line = "[{0:HH:mm:ss}] {1}" -f (Get-Date), $text; $line; Add-Content -Path $log -Value $line -Encoding UTF8 }

$target = $(if ($NoPublish) { "not published (upload later)" } else { $PublishDir })
Say "Overnight run started. Source: $Root (read-only). Work folder: $WorkDir. Shared copy: $target. Stops at $stop."
Say "Log: $log"
$round = 0
while ((Get-Date) -lt $stop) {
    $round++
    Say "Round ${round}: extracting up to $Batch new or changed files ..."
    $out = & .\start.bat drive_acquire.py --path $Root @excludes --max-files $Batch 2>&1 | Out-String
    Add-Content -Path $log -Value $out -Encoding UTF8
    $out.Trim().Split("`n") | Select-Object -Last 4 | ForEach-Object { Say "   $($_.Trim())" }
    if ($out -match "New or changed files to read: 0") { Say "Nothing new left to extract."; break }

    if ((Get-Date) -ge $stop) { Say "Stop time reached before embedding; the next run embeds this batch."; break }
    Say "Round ${round}: embedding into server.db (chunks + search vectors) ..."
    $out = & .\start.bat embed.py --documents 2>&1 | Out-String
    Add-Content -Path $log -Value $out -Encoding UTF8
    $out.Trim().Split("`n") | Select-Object -Last 3 | ForEach-Object { Say "   $($_.Trim())" }
    Publish
}
Publish
[void][Win32.Power]::SetThreadExecutionState([uint32]"0x80000000")
Say "Overnight run finished."