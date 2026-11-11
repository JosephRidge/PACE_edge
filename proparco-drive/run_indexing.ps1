# run_indexing.ps1 - make one extracted sector searchable (embeddings), robustly, until complete.
#
#   Double-click "RUN INDEXING.bat" (asks for the sector), or:
#   powershell -NoProfile -ExecutionPolicy Bypass -File run_indexing.ps1 -Sector ENI
#
# Reads C:\server-agent\<SECTOR>\acquisitions (made by RUN EXTRACTION) and builds
# C:\server-agent\<SECTOR>\db\server.db. Nothing on the server is touched.
#
# Built-in protections:
#   - sector folder missing on this laptop: found on the USB stick and copied here first
#   - pre-flight: program versions, Python, search model, disk space
#   - only one indexing run per sector at a time (lock file)
#   - leftover model servers from an earlier crash are stopped before each pass
#   - any stop or crash: restarts automatically and continues where it stopped
#   - sleep: kept awake while running; Windows restart: resumes at next logon
#   - a status file and a log for every pass

param(
    [string]$Sector = "",
    [string]$WorkBase = "C:\server-agent",
    [int]$MaxPasses = 20,
    [double]$MinFreeGB = 5,
    [switch]$NoResumeAtLogon,
    [switch]$Resumed
)

$ErrorActionPreference = "Continue"
$Drive = $PSScriptRoot
$Sectors = @("FII", "MAS", "ENI", "INV", "CROSS", "OLD", "ADMIN")
function Stamp { (Get-Date).ToString("HH:mm:ss") }

# ------------------------------------------------------------------ choose the sector
if (-not $Sector) {
    Write-Host ""
    for ($i = 0; $i -lt $Sectors.Count; $i++) { Write-Host ("  {0}. {1}" -f ($i + 1), $Sectors[$i]) }
    $choice = Read-Host "`nWhich sector to index? Type its number and press Enter"
    if ($choice -notmatch '^\d+$' -or [int]$choice -lt 1 -or [int]$choice -gt $Sectors.Count) {
        Write-Host "Not a valid choice. Nothing was done."; exit 1
    }
    $Sector = $Sectors[[int]$choice - 1]
}
$Sector = $Sector.ToUpper()
if ($Sectors -notcontains $Sector) { Write-Host "Unknown sector '$Sector'. Use one of: $($Sectors -join ', ')"; exit 1 }

$Work = Join-Path $WorkBase $Sector
$Acq = Join-Path $Work "acquisitions"

# ------------------------------------------------------------------ sector folder: here, or copy it from the stick
if (-not (Test-Path $Acq)) {
    $found = $null
    $bases = @((Split-Path $Drive -Parent), (Split-Path (Split-Path $Drive -Parent) -Parent))
    $bases += @(Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue | ForEach-Object { $_.Root })
    foreach ($base in $bases) {
        if ($base -and (Test-Path (Join-Path $base "$Sector\acquisitions"))) { $found = Join-Path $base $Sector; break }
    }
    if (-not $found) {
        Write-Host "`nNo extracted $Sector folder on this laptop ($Work) or on the stick."
        Write-Host "Run RUN EXTRACTION for $Sector first, or copy the $Sector folder to $WorkBase. Nothing was done."
        exit 1
    }
    Write-Host "`n$(Stamp)  Copying $found to $Work (one time; this can take a few minutes) ..."
    New-Item -ItemType Directory -Force $Work | Out-Null
    robocopy $found $Work /E /R:2 /W:5 /NFL /NDL /NJH /NP | Out-Null
    if ($LASTEXITCODE -ge 8 -or -not (Test-Path $Acq)) { Write-Host "Copy failed (robocopy code $LASTEXITCODE). Nothing was indexed."; exit 1 }
    Write-Host "$(Stamp)  Copied."
}

$Logs = Join-Path $Work "logs"
New-Item -ItemType Directory -Force $Logs, (Join-Path $Work "db") -ErrorAction SilentlyContinue | Out-Null
$RunLog = Join-Path $Logs ("indexing-{0:yyyyMMdd-HHmm}.log" -f (Get-Date))
$StatusFile = Join-Path $Work "indexing-status.json"

function Log($text) {
    $line = "$(Stamp)  $text"
    Write-Host $line
    Add-Content -Path $RunLog -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
}
function Status($state, $detail) {
    @{ sector = $Sector; state = $state; detail = $detail; updated = (Get-Date).ToString("s"); log = $RunLog } |
        ConvertTo-Json | Set-Content -Path $StatusFile -Encoding UTF8 -ErrorAction SilentlyContinue
}

Log "INDEXING sector $Sector  ->  $Work\db\server.db"

# ------------------------------------------------------------------ pre-flight
$problems = @()
$startBat = Join-Path $Drive "start.bat"
if (-not (Test-Path $startBat)) { $problems += "start.bat not found next to this script ($Drive)." }
if (-not (Test-Path (Join-Path $Drive "python\win\python.exe"))) { $problems += "Portable Python missing (python\win\python.exe)." }
if (-not (Test-Path (Join-Path $Drive "models\bge-m3-q8.gguf"))) { $problems += "Search model missing (models\bge-m3-q8.gguf): copy it from the PROPARCO stick." }
if (-not (Test-Path (Join-Path $Drive "bin\llama\llama-server.exe"))) { $problems += "Model engine missing (bin\llama\llama-server.exe)." }
$need = @{ "scripts\embed.py" = "CONFIDENTIAL_DOCS"; "scripts\engine.py" = "request_size" }
foreach ($f in $need.Keys) {
    $p = Join-Path $Drive $f
    if (-not (Test-Path $p)) { $problems += "$f missing." }
    elseif (-not (Select-String -Path $p -Pattern $need[$f] -SimpleMatch -Quiet)) { $problems += "$f is an old version: copy the latest from the stick." }
}
$batches = @(Get-ChildItem $Acq -Directory -ErrorAction SilentlyContinue).Count
if ($batches -eq 0) { $problems += "No extracted batches in $Acq." }
try {
    $free = (Get-PSDrive -Name ($Work.Substring(0, 1))).Free / 1GB
    if ($free -lt $MinFreeGB) { $problems += ("Only {0:N1} GB free on {1}: at least {2} GB needed." -f $free, $Work.Substring(0, 2), $MinFreeGB) }
} catch {}
if ($problems) {
    Log "CANNOT START:"; $problems | ForEach-Object { Log "  - $_" }
    Status "failed" ($problems -join " | "); exit 1
}
Log "Checks passed: program up to date, model present, $batches extracted batch(es)."

# ------------------------------------------------------------------ one run per sector (lock)
$Lock = Join-Path $Work "db\indexing.lock"
if (Test-Path $Lock) {
    $other = [int](Get-Content $Lock -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($other -and $other -ne $PID -and (Get-Process -Id $other -ErrorAction SilentlyContinue)) {
        Log "Another indexing run of $Sector is already running (process $other). Leave that one running; nothing was done."
        exit 1
    }
}
Set-Content -Path $Lock -Value $PID -Encoding ASCII

# ------------------------------------------------------------------ keep awake + resume at logon
$awake = $false
try {
    Add-Type -Namespace ProparcoIdx -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
'@ -ErrorAction Stop
    [void][ProparcoIdx.Power]::SetThreadExecutionState([uint32]"0x80000003"); $awake = $true
    Log "The laptop will not sleep and the screen stays on while this runs (closing the lid can still sleep it)."
} catch { Log "Note: could not keep the laptop awake automatically; set sleep to 'Never' while plugged in." }
$resumeFile = $null
if (-not $NoResumeAtLogon) {
    try {
        $resumeFile = Join-Path ([Environment]::GetFolderPath("Startup")) "Proparco indexing resume - $Sector.bat"
        $line = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Sector $Sector -WorkBase `"$WorkBase`" -Resumed"
        Set-Content -Path $resumeFile -Value "@echo off`r`n$line" -Encoding ASCII -ErrorAction Stop
        Log "If Windows restarts, indexing resumes at the next logon (removed when finished)."
    } catch { $resumeFile = $null }
}
function Finish($state, $detail, $code) {
    if ($awake) { [void][ProparcoIdx.Power]::SetThreadExecutionState([uint32]"0x80000000") }
    if ($resumeFile -and $state -eq "complete") { Remove-Item $resumeFile -ErrorAction SilentlyContinue }
    Remove-Item $Lock -ErrorAction SilentlyContinue
    Status $state $detail
    Log $detail
    exit $code
}
function Stop-LeftoverModel {
    # a model server left running by a crashed pass would block the next start
    try {
        Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -like "*bge-m3*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                             Log "  (stopped a leftover search-model process $($_.ProcessId))" }
    } catch {}
}

# ------------------------------------------------------------------ indexing passes
$env:PROPARCO_DATA_DIR = $Work
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
Set-Location $Drive

for ($pass = 1; $pass -le $MaxPasses; $pass++) {
    Stop-LeftoverModel
    Status "running" "pass $pass"
    Log "Pass ${pass}: indexing (progress lines appear after each group of documents is saved) ..."
    $passLog = Join-Path $Logs ("indexing-pass-{0:yyyyMMdd-HHmm}-{1}.log" -f (Get-Date), $pass)
    $lines = New-Object System.Collections.Generic.List[string]
    & $startBat embed.py --documents 2>&1 | ForEach-Object {
        $l = [string]$_; $lines.Add($l); Write-Host $l
        Add-Content -Path $passLog -Value $l -Encoding UTF8 -ErrorAction SilentlyContinue }
    $text = $lines -join "`n"

    if ($text -match "Nothing new to embed") {
        $sz = "{0:N0} MB" -f ((Get-Item (Join-Path $Work "db\server.db") -ErrorAction SilentlyContinue).Length / 1MB)
        Finish "complete" "SECTOR $Sector COMPLETELY INDEXED. Search database: $Work\db\server.db ($sz)." 0
    }
    if ($text -match "Done in [\d.]+ min") {
        Log "Pass $pass finished normally; one more pass to confirm nothing is left ..."
        continue
    }
    Log "Pass $pass stopped before the end (see $passLog). Restarting in 30 seconds; finished work is kept."
    Start-Sleep -Seconds 30
}
Finish "incomplete" "Stopped after $MaxPasses passes without completing. Run RUN INDEXING again to continue; see $Logs." 1