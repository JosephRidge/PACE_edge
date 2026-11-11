# run_extraction.ps1 - extract one sector of S:\Proparco (read-only), robustly, until complete.
#
#   Double-click "RUN EXTRACTION.bat" (asks for the sector), or:
#   powershell -NoProfile -ExecutionPolicy Bypass -File run_extraction.ps1 -Sector FII -StartAt 11:30
#
# Only EXTRACTS (reads files, saves their text in C:\server-agent\<SECTOR>). Chunking and
# embedding are a separate, later step. Nothing on the server is changed.
#
# Built-in mitigations (from problems met so far):
#   - pre-flight: program files and versions, Python, disk space, write access, server access
#   - server unreachable: waits and retries (also tries the drive's network path)
#   - network drop mid-run / any crash: re-runs automatically until nothing new is left
#   - files that can never be read: recognised as "complete with N unreadable" (no endless loop)
#   - sleep: kept awake while running; restart: resumes at next logon (removed when done)
#   - Python's messages merged before PowerShell sees them (no red NativeCommandError noise)
#   - UTF-8 everywhere (French names); a status file and a log for every pass

param(
    [string]$Sector = "",
    [string]$Root = "S:\Proparco",
    [string]$WorkBase = "C:\server-agent",
    [string]$StartAt = "",            # e.g. "11:30": wait until then before starting
    [double]$MaxHours = 40,           # stop trying after this long (resumes if run again)
    [double]$RetryMinutes = 5,           # wait between retries when the server is unreachable
    [double]$ServerWaitMinutes = 60,     # give up a server wait after this long (then retry later passes)
    [int]$MaxPasses = 30,
    [double]$MinFreeGB = 5,
    [switch]$NoResumeAtLogon,
    [switch]$Resumed
)

$ErrorActionPreference = "Continue"
$OnWindows = ($env:OS -eq "Windows_NT")
$Drive = $PSScriptRoot

$Sectors = [ordered]@{
    "FII"   = @("01. FII")
    "MAS"   = @("02. MAS")
    "ENI"   = @("03. ENI")
    "INV"   = @("04. INV")
    "CROSS" = @("05. Cross-sectors - Others", "08. Presentations", "CEO Forum 2026 & Africa Forward")
    "OLD"   = @("99. Old")
    "ADMIN" = @("06. Admin")
}

function Stamp { (Get-Date).ToString("HH:mm:ss") }

# ------------------------------------------------------------------ choose the sector
if (-not $Sector) {
    $keys = @($Sectors.Keys)
    Write-Host ""
    for ($i = 0; $i -lt $keys.Count; $i++) { Write-Host ("  {0}. {1}" -f ($i + 1), $keys[$i]) }
    $choice = Read-Host "`nWhich sector? Type its number and press Enter"
    if ($choice -notmatch '^\d+$' -or [int]$choice -lt 1 -or [int]$choice -gt $keys.Count) {
        Write-Host "Not a valid choice. Nothing was done."; exit 1
    }
    $Sector = $keys[[int]$choice - 1]
}
$Sector = $Sector.ToUpper()
if (-not $Sectors.Contains($Sector)) { Write-Host "Unknown sector '$Sector'. Use one of: $($Sectors.Keys -join ', ')"; exit 1 }

$Work = Join-Path $WorkBase $Sector
$Logs = Join-Path $Work "logs"
New-Item -ItemType Directory -Force $Logs -ErrorAction SilentlyContinue | Out-Null
$RunLog = Join-Path $Logs ("run-{0:yyyyMMdd-HHmm}.log" -f (Get-Date))
$StatusFile = Join-Path $Work "status.json"

function Log($text) {
    $line = "[$(Stamp)] $text"
    Write-Host $line
    Add-Content -Path $RunLog -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}
function Status($state, $detail, $extra = @{}) {
    $s = [ordered]@{ sector = $Sector; state = $state; detail = $detail
                     updated = (Get-Date).ToString("s"); log = $RunLog }
    foreach ($k in $extra.Keys) { $s[$k] = $extra[$k] }
    ($s | ConvertTo-Json -Depth 3) | Set-Content -Path $StatusFile -Encoding UTF8 -ErrorAction SilentlyContinue
}

Log "================================================================"
Log "Proparco extraction - sector $Sector$(if ($Resumed) { ' (resumed after restart)' })"
Log "================================================================"

# ------------------------------------------------------------------ pre-flight checks
$problems = @()
$python = $null
if ($OnWindows) {
    $startBat = Join-Path $Drive "start.bat"
    if (-not (Test-Path $startBat)) { $problems += "start.bat not found next to this script ($Drive)." }
    # The portable Python carried on the drive: nothing is installed on this laptop.
    $portable = Join-Path $Drive "python\win\python.exe"
    if (-not (Test-Path $portable)) {
        $problems += "The drive's portable Python is missing ($portable). Copy the complete proparco-drive folder (it includes Python; nothing gets installed)."
    }
} else {
    $python = (Get-Command python3 -ErrorAction SilentlyContinue).Source
    if (-not $python) { $problems += "python3 not found." }
}
$acq = Join-Path $Drive "scripts\drive_acquire.py"
if (-not $OnWindows) { $acq = Join-Path $Drive "scripts/drive_acquire.py" }
if (-not (Test-Path $acq)) {
    $problems += "scripts\drive_acquire.py not found."
} elseif (-not (Select-String -Path $acq -Pattern "library not yet installed" -SimpleMatch -Quiet)) {
    $problems += "scripts\drive_acquire.py is an OLD version: copy the latest one first."
}
if ($OnWindows -and -not (Test-Path (Join-Path $Drive "python\win\Lib\site-packages\Crypto"))) {
    Log "Note: the PDF decryption library is not on this drive: encrypted PDFs will be skipped now and retried once it is added."
}
try {
    $probe = Join-Path $Work ".write-test"
    Set-Content -Path $probe -Value "ok" -ErrorAction Stop; Remove-Item $probe -ErrorAction SilentlyContinue
} catch { $problems += "Cannot write to $Work ($($_.Exception.Message))." }
try {
    $qualifier = Split-Path -Qualifier $Work -ErrorAction Stop
    $free = (Get-PSDrive -Name $qualifier.TrimEnd(":") -ErrorAction Stop).Free / 1GB
} catch {
    $free = [double]((Get-PSDrive -PSProvider FileSystem | Where-Object { $Work.StartsWith($_.Root) } |
                     Sort-Object { $_.Root.Length } -Descending | Select-Object -First 1).Free) / 1GB
}
if ($free -and $free -lt $MinFreeGB) { $problems += ("Only {0:N1} GB free for {1}; at least {2} GB needed." -f $free, $Work, $MinFreeGB) }
if ($problems) {
    Log "CANNOT START:"; $problems | ForEach-Object { Log "  - $_" }
    Status "failed" ($problems -join " ")
    exit 1
}
Log ("Pre-flight OK: program files current, portable Python on the drive (nothing installed), {0:N0} GB free, work folder {1}" -f $free, $Work)

# ------------------------------------------------------------------ server access (with fallback path)
function Resolve-ServerRoot {
    if (Test-Path -LiteralPath $Root) { return $Root }
    if ($OnWindows -and $Root -match '^([A-Za-z]):\\(.*)$') {
        $letter = $Matches[1]; $rest = $Matches[2]
        $unc = $null
        try { $unc = (Get-ItemProperty "HKCU:\Network\$letter" -ErrorAction Stop).RemotePath } catch {}
        if (-not $unc) {
            try { $unc = (Get-CimInstance Win32_MappedLogicalDisk -ErrorAction Stop |
                          Where-Object { $_.DeviceID -eq "${letter}:" }).ProviderName } catch {}
        }
        if ($unc) {
            $alt = Join-Path $unc $rest
            if (Test-Path -LiteralPath $alt) { return $alt }
        }
    }
    return $null
}
function Wait-ForServer {
    $deadline = (Get-Date).AddMinutes($ServerWaitMinutes)
    while ($true) {
        $r = Resolve-ServerRoot
        if ($r) { return $r }
        if ((Get-Date) -ge $deadline) { return $null }
        Status "waiting-network" "server not reachable; retrying every $RetryMinutes min"
        Log "The server ($Root) is not reachable. Waiting $RetryMinutes minutes, then trying again ..."
        Start-Sleep -Seconds ([int][Math]::Max(1, $RetryMinutes * 60))
    }
}

# ------------------------------------------------------------------ keep awake + resume at logon
$awake = $false
if ($OnWindows) {
    try {
        Add-Type -Namespace ProparcoWin32 -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
'@ -ErrorAction Stop
        [void][ProparcoWin32.Power]::SetThreadExecutionState([uint32]"0x80000003"); $awake = $true
    } catch { Log "Note: could not keep the laptop awake automatically; set sleep to 'Never' while plugged in." }
    if ($awake) { Log "The laptop will not sleep and the screen stays on while this runs (closing the lid can still sleep it)." }
}
$resumeFile = $null
if ($OnWindows -and -not $NoResumeAtLogon) {
    try {
        $startup = [Environment]::GetFolderPath("Startup")
        $resumeFile = Join-Path $startup "Proparco extraction resume - $Sector.bat"
        $line = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Sector $Sector -Root `"$Root`" -WorkBase `"$WorkBase`" -Resumed"
        Set-Content -Path $resumeFile -Value "@echo off`r`n$line" -Encoding ASCII -ErrorAction Stop
        Log "If Windows restarts, the extraction resumes at the next logon (removed when finished)."
    } catch { $resumeFile = $null; Log "Note: resume-at-logon could not be set up ($($_.Exception.Message))." }
}
function Finish($state, $detail, $code, $extra = @{}) {
    if ($awake) { [void][ProparcoWin32.Power]::SetThreadExecutionState([uint32]"0x80000000") }
    if ($resumeFile -and $state -eq "complete") { Remove-Item $resumeFile -ErrorAction SilentlyContinue }
    Status $state $detail $extra
    Log $detail
    exit $code
}

# ------------------------------------------------------------------ wait for the start time
if ($StartAt -and -not $Resumed) {
    $t = [datetime]::Today.Add([TimeSpan]::Parse($StartAt))
    if ($t -gt (Get-Date)) {
        Status "scheduled" "waiting until $StartAt"
        Log "Waiting until $StartAt to start (keep this window open) ..."
        while ((Get-Date) -lt $t) { Start-Sleep -Seconds 30 }
    }
}

# ------------------------------------------------------------------ extraction passes
$env:PROPARCO_DATA_DIR = $Work
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$deadline = (Get-Date).AddHours($MaxHours)
$totalRead = 0; $previousRemaining = -1; $pass = 0
while ($pass -lt $MaxPasses) {
    $pass++
    if ((Get-Date) -ge $deadline) { Finish "stopped" "Stopped after $MaxHours h; run again to continue." 2 }
    $server = Wait-ForServer
    if (-not $server) {
        Log "The server stayed unreachable for $ServerWaitMinutes min; trying again shortly."
        continue
    }
    $paths = @()
    foreach ($f in $Sectors[$Sector]) {
        $p = Join-Path $server $f
        if (Test-Path -LiteralPath $p) { $paths += $p } else { Log "  (not found, skipped: $p)" }
    }
    if (-not $paths) { Finish "failed" "No folder of sector $Sector exists under $server." 1 }

    Status "running" "pass $pass" @{ pass = $pass; documents_read_so_far = $totalRead }
    Log "Pass ${pass}: reading $($paths -join ', ') ..."
    $passLog = Join-Path $Logs ("pass-{0:yyyyMMdd-HHmm}-{1}.log" -f (Get-Date), $pass)
    $lines = New-Object System.Collections.Generic.List[string]
    if ($OnWindows) {
        # Called exactly as by hand (.\start.bat drive_acquire.py --path "S:\...") and PowerShell
        # quotes each argument itself: no hand-built cmd.exe command line (spaces, accents).
        $dargs = @()
        foreach ($p in $paths) { $dargs += "--path"; $dargs += $p }
        $dargs += "--exclude"; $dargs += ".pst"
        & $startBat drive_acquire.py @dargs 2>&1 | ForEach-Object {
            $l = [string]$_; $lines.Add($l); Write-Host $l
            Add-Content -Path $passLog -Value $l -Encoding UTF8 -ErrorAction SilentlyContinue }
    } else {
        $pyArgs = @($acq) + ($paths | ForEach-Object { "--path"; $_ }) + @("--exclude", ".pst")
        & $python @pyArgs 2>&1 | ForEach-Object {
            $l = [string]$_; $lines.Add($l); Write-Host $l
            Add-Content -Path $passLog -Value $l -Encoding UTF8 -ErrorAction SilentlyContinue }
    }
    $text = $lines -join "`n"
    $toRead = if ($text -match "New or changed files to read:\s*([\d,]+)") { [int]($Matches[1] -replace ",", "") } else { -1 }
    # the final summary line ("N documents read in B batch(es)"), not the progress lines
    $read = if ($text -match "(?m)^([\d,]+) documents read in \d+ batch") { [int]($Matches[1] -replace ",", "") } else { -1 }
    if ($read -gt 0) { $totalRead += $read }

    if ($toRead -eq 0) {
        Finish "complete" "SECTOR $Sector COMPLETELY EXTRACTED (nothing new to read). Documents read in this session: $totalRead." 0 @{ documents_read = $totalRead }
    }
    if ($toRead -gt 0 -and $read -eq 0) {
        # nothing could be read this pass: the remaining files are unreadable (damaged, locked...)
        Finish "complete" "SECTOR $Sector EXTRACTED: everything readable is done; $toRead file(s) cannot be read (listed in $passLog)." 0 @{ documents_read = $totalRead; unreadable = $toRead }
    }
    if ($toRead -ge 0 -and $read -ge 0) {
        if ($toRead -eq $previousRemaining) {
            Finish "complete" "SECTOR $Sector EXTRACTED: no further progress possible; see $passLog for the files that could not be read." 0 @{ documents_read = $totalRead }
        }
        $previousRemaining = $toRead
        Log "Pass $pass finished ($read documents read). Checking for anything left ..."
        continue
    }
    # the pass did not finish normally (crash, network drop during listing ...): wait, then retry
    Log "Pass $pass did not finish normally; retrying in 2 minutes (details: $passLog)."
    Status "retrying" "pass $pass interrupted" @{ pass = $pass }
    Start-Sleep -Seconds ([int][Math]::Max(1, [Math]::Min(120, $RetryMinutes * 60)))
}
Finish "stopped" "Stopped after $MaxPasses passes; run again to continue." 2