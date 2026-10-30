<#
outlook_acquire.ps1 - export Outlook messages, threads and attachments into the common
acquisition format (acquisition.json + manifest.json + attachments\), the same format the
Gmail export writes. embed.py and generate.py then work unchanged.

Version 2. Changes from OutlookAcquisition.ps1:
  - real SMTP addresses for internal (Exchange) senders and recipients, plus display names
  - attachments always written as a list
  - thread ID (ConversationID) and thread topic on every message
  - -Selected mode: export only the emails selected in Outlook (assisted scenario)
  - UTF-8 output without byte-order mark, ISO dates

Requires Outlook desktop (classic), open and signed in. Read-only: nothing in the mailbox
is changed, moved or marked as read.

Examples (PowerShell):
  .\outlook_acquire.ps1 -Selected
  .\outlook_acquire.ps1 -DaysBack 30
  .\outlook_acquire.ps1 -DaysBack 90 -Folders Inbox,SentMail -MaxMessages 500
  .\outlook_acquire.ps1 -DaysBack 30 -NoAttachments

If Windows refuses to run scripts, ask IT; do not change the execution policy on a
managed laptop.
#>
param(
    [switch]$Selected,
    [int]$DaysBack = 30,
    [ValidateSet("Inbox", "SentMail")]
    [string[]]$Folders = @("Inbox", "SentMail"),
    [int]$MaxMessages = 1000,
    [int]$MaxAttachmentMB = 20,
    [switch]$NoAttachments,
    [string]$OutRoot = (Join-Path $HOME "proparco-data\acquisitions")
)

$ErrorActionPreference = "Stop"
$PR_SMTP_ADDRESS        = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"
$PR_SENDER_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"
$FolderIds = @{ Inbox = 6; SentMail = 5 }      # olFolderInbox, olFolderSentMail
$olMail = 43; $olTo = 1; $olCC = 2

# ---------------------------------------------------------------- helpers

function Get-Smtp($entry) {
    # An address entry -> its SMTP address, also for internal Exchange users.
    if ($null -eq $entry) { return $null }
    try { if ($entry.Type -eq "SMTP") { return $entry.Address } } catch {}
    try { $u = $entry.GetExchangeUser(); if ($u -and $u.PrimarySmtpAddress) { return $u.PrimarySmtpAddress } } catch {}
    try { $d = $entry.GetExchangeDistributionList(); if ($d -and $d.PrimarySmtpAddress) { return $d.PrimarySmtpAddress } } catch {}
    try { return $entry.PropertyAccessor.GetProperty($PR_SMTP_ADDRESS) } catch {}
    return $null
}

function Get-SenderSmtp($mail) {
    try { if ($mail.SenderEmailType -ne "EX") { return $mail.SenderEmailAddress } } catch {}
    try { $s = Get-Smtp $mail.Sender; if ($s) { return $s } } catch {}
    try { return $mail.PropertyAccessor.GetProperty($PR_SENDER_SMTP_ADDRESS) } catch {}
    return $null
}

function Format-Address([string]$name, [string]$smtp) {
    if ($smtp) {
        if ($name -and $name -ne $smtp) { return "$name <$smtp>" }
        return $smtp
    }
    return $name
}

function Get-SafeName([string]$name) {
    $clean = ($name -replace '[\\/:*?"<>|\x00-\x1f]', '_').Trim(' ', '.')
    if (-not $clean) { $clean = "attachment" }
    if ($clean.Length -gt 120) { $clean = $clean.Substring(0, 120) }
    return $clean
}

function Get-IsoDate($value) {
    try { return ([datetime]$value).ToString("yyyy-MM-ddTHH:mm:ss") } catch { return $null }
}

$script:AttCounter = 0
function Convert-Mail($m, [string]$folderName) {
    $to = @(); $cc = @()
    foreach ($r in $m.Recipients) {
        $addr = Format-Address $r.Name (Get-Smtp $r.AddressEntry)
        if ($r.Type -eq $olCC) { $cc += $addr } elseif ($r.Type -eq $olTo) { $to += $addr }
    }

    $atts = @()
    foreach ($a in $m.Attachments) {
        $size = [int64]$a.Size
        $rec = [ordered]@{ filename = $a.FileName; sizeBytes = $size
                           sizeMB = [math]::Round($size / 1MB, 3); acquired = $false
                           localPath = $null; skipReason = $null }
        if ($NoAttachments) {
            $rec.skipReason = "attachments not requested"
        } elseif ($size -gt $MaxAttachmentMB * 1MB) {
            $rec.skipReason = "larger than $MaxAttachmentMB MB"
        } else {
            try {
                $script:AttCounter++
                $path = Join-Path $AttDir ("{0:D5}_{1}" -f $script:AttCounter, (Get-SafeName $a.FileName))
                $a.SaveAsFile($path)
                $rec.acquired = $true; $rec.localPath = $path
            } catch { $rec.skipReason = "could not save: $($_.Exception.Message)" }
        }
        $atts += $rec
    }

    $senderSmtp = Get-SenderSmtp $m
    return [ordered]@{
        source         = "outlook"
        entryId        = $m.EntryID
        conversationId = $m.ConversationID
        threadTopic    = $m.ConversationTopic
        subject        = $m.Subject
        sender         = Format-Address $m.SenderName $senderSmtp
        senderName     = $m.SenderName
        senderEmail    = $(if ($senderSmtp) { $senderSmtp.ToLower() } else { $null })
        to             = $to
        cc             = $cc
        receivedTime   = $(if ($folderName -eq "SentMail") { Get-IsoDate $m.SentOn } else { Get-IsoDate $m.ReceivedTime })
        folder         = $folderName
        body           = $m.Body
        attachments    = $atts
    }
}

# ---------------------------------------------------------------- run

$started = Get-Date
$runId   = "OUTLOOK-" + $started.ToString("yyyy-MM-dd_HH-mm-ss")
$RunDir  = Join-Path $OutRoot $runId
$AttDir  = Join-Path $RunDir "attachments"
New-Item -ItemType Directory -Force $AttDir | Out-Null

$outlook = New-Object -ComObject Outlook.Application
$ns = $outlook.GetNamespace("MAPI")

$messages = New-Object System.Collections.ArrayList
$seen = @{}; $skipped = 0; $searched = @()

function Add-Mail($m, [string]$folderName) {
    if ($m.Class -ne $olMail) { $script:skipped++; return }
    if ($seen.ContainsKey($m.EntryID)) { return }
    try {
        $rec = Convert-Mail $m $folderName
        $seen[$m.EntryID] = $true
        [void]$messages.Add($rec)
        if ($messages.Count % 50 -eq 0) { Write-Host "  $($messages.Count) messages ..." }
    } catch {
        $script:skipped++
        Write-Warning "Skipped one item: $($_.Exception.Message)"
    }
}

if ($Selected) {
    $explorer = $outlook.ActiveExplorer()
    if ($null -eq $explorer -or $explorer.Selection.Count -eq 0) {
        throw "No emails are selected in Outlook. Select them first, then run again."
    }
    $searched = @("Selection in Outlook")
    foreach ($m in $explorer.Selection) {
        if ($messages.Count -ge $MaxMessages) { break }
        Add-Mail $m "Selection"
    }
} else {
    $since = (Get-Date).AddDays(-$DaysBack)
    # Use the mailbox open on screen: the folder shown in Outlook plus that mailbox's Sent folder.
    # (The "default" mailbox can be a different, empty one when several accounts are configured.)
    $targets = @()
    try {
        $explorer = $outlook.ActiveExplorer()
        if ($explorer -and $explorer.CurrentFolder) {
            $cur = $explorer.CurrentFolder
            $targets += ,@($cur, "Inbox")
            try { $targets += ,@($cur.Store.GetDefaultFolder(5), "SentMail") } catch {}
        }
    } catch {}
    if ($targets.Count -eq 0) {
        foreach ($f in $Folders) { $targets += ,@($ns.GetDefaultFolder($FolderIds[$f]), $f) }
    }
    foreach ($pair in $targets) {
        $folder = $pair[0]; $f = $pair[1]
        if ($Folders -notcontains $f) { continue }
        $searched += $folder.Name
        $field = $(if ($f -eq "SentMail") { "[SentOn]" } else { "[ReceivedTime]" })
        # No date filter string (Outlook reads it in the laptop's regional format, which can
        # silently match nothing). Sort newest first and stop at the first older item instead.
        $items = $folder.Items
        $items.Sort($field, $true)
        foreach ($m in $items) {
            if ($messages.Count -ge $MaxMessages) { break }
            $when = $null
            try { $when = $(if ($f -eq "SentMail") { $m.SentOn } else { $m.ReceivedTime }) } catch {}
            if ($when -and ([datetime]$when) -lt $since) { break }
            Add-Mail $m $f
        }
    }
}

$acquiredAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$threads = @($messages | ForEach-Object { $_.conversationId } | Sort-Object -Unique).Count
$saved = @($messages | ForEach-Object { $_.attachments } | Where-Object { $_.acquired }).Count

$acquisition = [ordered]@{
    source            = "outlook"
    acquisitionMethod = "Outlook COM v2"
    scope             = $(if ($Selected) { "selected emails" } else { "last $DaysBack days: $($Folders -join ', ')" })
    acquiredAt        = $acquiredAt
    messages          = $messages.ToArray()
}
$manifest = [ordered]@{
    runId                     = $runId
    source                    = "outlook"
    scenario                  = $(if ($Selected) { "assisted" } else { "bulk" })
    trigger                   = "manual"
    initiatedBy               = $env:USERNAME
    acquiredAt                = $acquiredAt
    durationSeconds           = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    daysBack                  = $(if ($Selected) { $null } else { $DaysBack })
    messageCount              = $messages.Count
    threadCount               = $threads
    attachmentsSaved          = $saved
    skippedItemCount          = $skipped
    maxMessages               = $MaxMessages
    cappedByMaxMessages       = ($messages.Count -ge $MaxMessages)
    attachmentContentIncluded = (-not $NoAttachments)
    maxAttachmentSizeMB       = $MaxAttachmentMB
    foldersSearched           = $searched
    outputFolder              = $RunDir
}

# UTF-8 without byte-order mark; depth high enough for attachments inside messages.
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText((Join-Path $RunDir "acquisition.json"), ($acquisition | ConvertTo-Json -Depth 8), $utf8)
[System.IO.File]::WriteAllText((Join-Path $RunDir "manifest.json"), ($manifest | ConvertTo-Json -Depth 4), $utf8)

Write-Host ""
Write-Host "$($messages.Count) messages in $threads threads, $saved attachments saved, $skipped skipped."
Write-Host "Written to $RunDir"
Write-Host "Next: run 'Embed new emails' from the menu (or embed.py --dry-run)."