# extract_mail.ps1 - export my emails: the mailbox and every archive, completely. Started by "EXTRACT MY EMAILS.bat".
#
# Lists every mailbox and archive open in classic Outlook, then exports the chosen ones (default:
# all except public folders), read-only: all folders, all dates, every item type, every attachment
# whatever its size. Ends with a per-folder check against Outlook's own counts
# ("ALL ITEMS ACCOUNTED FOR"). Interrupted? Run again: nothing already exported is exported twice,
# also when the same email sits in the mailbox and in an archive. No indexing yet.

Set-Location $PSScriptRoot
$data = Join-Path (Split-Path $PSScriptRoot -Parent) "proparco-data"
$out = Join-Path $data "acquisitions"
New-Item -ItemType Directory -Force $out, (Join-Path $data "logs") | Out-Null
$log = Join-Path $data ("logs\mail-export-{0:yyyyMMdd-HHmm}.log" -f (Get-Date))

Add-Type -Namespace Win32 -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
'@
[void][Win32.Power]::SetThreadExecutionState([uint32]"0x80000003")

Write-Host ""
Write-Host "================================================================"
Write-Host "  Export my emails (read-only): the mailbox and every archive"
Write-Host "================================================================"
Write-Host "`nKeep classic Outlook open. If Outlook asks to allow access, click Allow."
Write-Host "Archives kept as .pst files must be open in Outlook first (File > Open & Export > Open Outlook Data File)."
try {
    $freeGB = (Get-PSDrive -Name $data.Substring(0, 1)).Free / 1GB
    Write-Host ("Free space on this flash disk: {0:N1} GB (attachments are saved in full; the export stops cleanly if it runs low)." -f $freeGB)
} catch {}

# ---------------------------------------------------------------- what is open in Outlook
try {
    $ol = New-Object -ComObject Outlook.Application
    # one entry at a time: a single unreadable entry (disconnected shared mailbox, missing .pst)
    # must not hide all the others
    $coll = $ol.GetNamespace("MAPI").Stores
    $count = 0; try { $count = [int]$coll.Count } catch {}
    $stores = @(); $bad = @()
    for ($k = 1; $k -le $count; $k++) {
        try {
            $st = $coll.Item($k)
            $name = [string]$st.DisplayName
            $type = $(try { [int]$st.ExchangeStoreType } catch { 3 })
            $stores += [pscustomobject]@{ Name = $name; Public = ($type -eq 2)
                Kind = $(switch ($type) { 0 { "mailbox" } 1 { "other person's mailbox" } 2 { "public folders" }
                                          3 { "data file (.pst)" } default { "archive / additional mailbox" } }) }
        } catch { $bad += "entry $k of ${count}: $($_.Exception.Message)" }
    }
    if (-not $stores.Count) { throw "Outlook listed no readable mailbox ($count entries; $($bad -join '; '))" }
} catch {
    Write-Host "`nCould not talk to Outlook: $($_.Exception.Message)"
    Write-Host "Open classic Outlook (not the new Outlook), then run this again."
    [void][Win32.Power]::SetThreadExecutionState([uint32]"0x80000000"); exit 1
}
Write-Host "`nOpen in Outlook:"
if ($bad) {
    Write-Host "  Outlook could not read $($bad.Count) entr$(if ($bad.Count -eq 1) { 'y' } else { 'ies' }) of its list (often a disconnected shared mailbox or a"
    Write-Host "  .pst that has moved). Check that every mailbox and archive she uses appears below:"
    $bad | ForEach-Object { Write-Host "    - $_" }
}
for ($i = 0; $i -lt $stores.Count; $i++) {
    Write-Host ("  {0}. {1}   ({2}){3}" -f ($i + 1), $stores[$i].Name, $stores[$i].Kind, $(if ($stores[$i].Public) { "  - not exported unless chosen" } else { "" }))
}
$default = @($stores | Where-Object { -not $_.Public })
$answer = Read-Host "`nPress Enter to export all of these except public folders, or type the numbers to export (e.g. 1,2)"
if ($answer.Trim()) {
    $picked = @($answer -split '[,; ]+' | Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ - 1 } |
                Where-Object { $_ -ge 0 -and $_ -lt $stores.Count } | ForEach-Object { $stores[$_] })
} else { $picked = $default }
if (-not $picked) { Write-Host "Nothing chosen. Nothing was exported."; [void][Win32.Power]::SetThreadExecutionState([uint32]"0x80000000"); exit 1 }
Write-Host "`nExporting: $(($picked | ForEach-Object { $_.Name }) -join ' | ')"
Write-Host "Large mailboxes take hours; progress is shown below. Keep this window and Outlook open.`n"

# ---------------------------------------------------------------- export
$names = @($picked | ForEach-Object { $_.Name })
$text = & powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\scripts\outlook_acquire.ps1" `
    -Complete -StoreList ($names -join "|") -OutRoot $out 2>&1 | ForEach-Object { $l = [string]$_; Write-Host "  $l"; $l } | Out-String
Add-Content -Path $log -Value $text -Encoding UTF8

$line = ($text -split "`n" | Where-Object { $_ -match "messages in .* threads" } | Select-Object -Last 1)
$check = ($text -split "`n" | Where-Object { $_ -match "^(ALL ITEMS ACCOUNTED FOR|ACCOUNTED FOR WITH EXCEPTIONS|CHECK:|STOPPED:)" } | Select-Object -Last 1)
[void][Win32.Power]::SetThreadExecutionState([uint32]"0x80000000")
Write-Host "`n================================================================"
Write-Host "  Email export summary"
Write-Host "  Exported from: $($names -join ' | ')"
if ($line) { Write-Host "  $($line.Trim())" } else { Write-Host "  The export did not complete: see the log." }
if ($check) { Write-Host "  $($check.Trim())" }
Write-Host "  Saved on this flash disk: $out"
Write-Host "  Log: $log"
Write-Host "================================================================"
Add-Content -Path $log -Value ("SUMMARY`nExported from: $($names -join ' | ')`n$line`n$check") -Encoding UTF8
