<#
outlook_acquire.ps1 - export Outlook items, threads and attachments into the common
acquisition format (acquisition.json + manifest.json + attachments\), the same format the
Gmail export writes. embed.py and generate.py then work unchanged.

Version 3 (complete export). Changes from version 2:
  - -Complete: every item in every mail folder below the folder open in Outlook, all dates,
    all attachment sizes, every item type (meeting requests and responses, delivery reports,
    posts...), with the Outlook type recorded on each item
  - written in parts of -PartSize items (default 1000): memory stays small, a crash loses at
    most the current part
  - resumable: exported item IDs are recorded (exported-ids.txt); a re-run, or the same email
    met again in an archive, is not exported twice
  - per-folder reconciliation against Outlook's own item counts, ending with
    "ALL ITEMS ACCOUNTED FOR" or the exact list of items that failed and why
  - stops cleanly when the disk is nearly full (re-run to continue)

Requires Outlook desktop (classic), open and signed in. Read-only: nothing in the mailbox
is changed, moved or marked as read.

Examples (PowerShell):
  .\outlook_acquire.ps1 -Selected                 (emails selected in Outlook)
  .\outlook_acquire.ps1 -Complete                 (everything below the folder open in Outlook)
  .\outlook_acquire.ps1 -DaysBack 30              (last 30 days of Inbox and Sent)

If Windows refuses to run scripts, ask IT; do not change the execution policy on a
managed laptop.
#>
param(
    [switch]$Selected,
    [switch]$Complete,
    [int]$DaysBack = 30,              # 0 = all dates
    [ValidateSet("Inbox", "SentMail")]
    [string[]]$Folders = @("Inbox", "SentMail"),
    [int]$MaxMessages = 1000,         # 0 = no cap
    [int]$MaxAttachmentMB = 20,       # 0 = no size limit
    [switch]$NoAttachments,
    [switch]$IncludeSubfolders,
    [string[]]$Stores = @(),          # with -Complete: export these mailboxes/archives by display name
    [string]$StoreList = "",          # the same as one "|"-separated value (safe through powershell -File)
    [int]$PartSize = 1000,
    [double]$MinFreeGB = 2,
    [string]$OutRoot = (Join-Path $HOME "proparco-data\acquisitions")
)

$ErrorActionPreference = "Stop"
if ($StoreList) { $Stores = @($StoreList -split '\|' | Where-Object { $_ }) }
if ($Complete) { $DaysBack = 0; $MaxMessages = 0; $MaxAttachmentMB = 0; $IncludeSubfolders = $true }
# A selection is exported exactly as selected, every time: no date limit, cap or size limit, every
# item type, and no resume memory (the one-click summary needs a fresh export of what she picked;
# the indexer already ignores emails it has seen before).
if ($Selected) { $DaysBack = 0; $MaxMessages = 0; $MaxAttachmentMB = 0 }
$PR_SMTP_ADDRESS        = "http://schemas.microsoft.com/mapi/proptag/0x39FE001F"
$PR_SENDER_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x5D01001F"
$FolderIds = @{ Inbox = 6; SentMail = 5 }      # olFolderInbox, olFolderSentMail
$olMail = 43; $olTo = 1; $olCC = 2
$PR_INTERNET_MESSAGE_ID = "http://schemas.microsoft.com/mapi/proptag/0x1035001F"
$PR_ATTACHMENT_HIDDEN   = "http://schemas.microsoft.com/mapi/proptag/0x7FFE000B"
$PR_ATTACH_CONTENT_ID   = "http://schemas.microsoft.com/mapi/proptag/0x3712001F"

# ---------------------------------------------------------------- helpers

function P([scriptblock]$get) { try { return (& $get) } catch { return $null } }

# Outlook collections are read one entry at a time: enumerating a whole collection fails as soon as
# ONE entry is unreadable (a disconnected shared mailbox, a missing .pst, a damaged item), and that
# would silently drop everything after it. Unreadable entries are named in the summary instead.
function Get-N($coll) {
    if ($null -eq $coll) { return 0 }
    if ($coll -is [array]) { return $coll.Count }
    try { return [int]$coll.Count } catch { return 0 }
}
function Get-At($coll, [int]$i) { if ($coll -is [array]) { return ,$coll[$i - 1] }; return ,$coll.Item($i) }
$script:unreadable = New-Object System.Collections.ArrayList

$script:smtpCache = @{}
function Get-Smtp($entry) {
    # An address entry -> its SMTP address, also for internal Exchange users. Cached: resolving an
    # internal user is a directory lookup, and the same people appear in thousands of emails.
    if ($null -eq $entry) { return $null }
    $key = $null; try { $key = [string]$entry.ID } catch {}
    if ($key -and $script:smtpCache.ContainsKey($key)) { return $script:smtpCache[$key] }
    $v = Resolve-Smtp $entry
    if ($key) { $script:smtpCache[$key] = $v }
    return $v
}

function Resolve-Smtp($entry) {
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
function Convert-Item($m, [string]$folderName, [string]$folderPath) {
    # Every property read is guarded: item types without a sender, recipients or received
    # time (delivery reports, some meeting responses) are exported with those fields empty.
    $to = @(); $cc = @()
    try {
        foreach ($r in $m.Recipients) {
            $addr = Format-Address (P { $r.Name }) (P { Get-Smtp $r.AddressEntry })
            $type = P { $r.Type }
            if ($type -eq $olCC) { $cc += $addr } elseif ($type -eq $olTo) { $to += $addr }
        }
    } catch {}

    $atts = @()
    try {
        foreach ($a in $m.Attachments) {
            $size = [int64](P { $a.Size })
            $name = [string](P { $a.FileName })
            if (-not $name) { $name = [string](P { $a.DisplayName }) }
            $hidden = P { $a.PropertyAccessor.GetProperty($PR_ATTACHMENT_HIDDEN) }
            $cid = P { $a.PropertyAccessor.GetProperty($PR_ATTACH_CONTENT_ID) }
            $rec = [ordered]@{ filename = $name; sizeBytes = $size
                               sizeMB = [math]::Round($size / 1MB, 3); acquired = $false
                               inline = [bool]($hidden -or $cid)    # signature logos, pasted images: saved too
                               localPath = $null; skipReason = $null }
            if ($NoAttachments) {
                $rec.skipReason = "attachments not requested"
            } elseif ($MaxAttachmentMB -gt 0 -and $size -gt $MaxAttachmentMB * 1MB) {
                $rec.skipReason = "larger than $MaxAttachmentMB MB"
            } else {
                try {
                    $script:AttCounter++
                    $path = Join-Path $script:AttDir ("{0:D6}_{1}" -f $script:AttCounter, (Get-SafeName $name))
                    $a.SaveAsFile($path)
                    $rec.acquired = $true; $rec.localPath = $path
                } catch { $rec.skipReason = "could not save: $($_.Exception.Message)"; $script:AttFailed++ }
            }
            $atts += $rec
        }
    } catch {}

    $entryId = [string](P { $m.EntryID })
    $conv = [string](P { $m.ConversationID })
    if (-not $conv) { $conv = "single:" + $entryId }      # never merge unrelated items into one thread
    $when = $(if ($folderName -eq "SentMail") { P { $m.SentOn } } else { P { $m.ReceivedTime } })
    if (-not $when) { $when = P { $m.SentOn } }
    if (-not $when) { $when = P { $m.CreationTime } }
    $body = P { $m.Body }
    if (-not $body) { $script:noBody++ }
    $senderSmtp = P { Get-SenderSmtp $m }
    $senderName = P { $m.SenderName }
    return [ordered]@{
        source         = "outlook"
        entryId        = $entryId
        messageId      = P { $m.PropertyAccessor.GetProperty($PR_INTERNET_MESSAGE_ID) }
        conversationId = $conv
        threadTopic    = P { $m.ConversationTopic }
        itemClass      = P { $m.MessageClass }
        subject        = P { $m.Subject }
        sender         = Format-Address $senderName $senderSmtp
        senderName     = $senderName
        senderEmail    = $(if ($senderSmtp) { ([string]$senderSmtp).ToLower() } else { $null })
        to             = $to
        cc             = $cc
        receivedTime   = Get-IsoDate $when
        folder         = $folderName
        folderPath     = $folderPath
        body           = $body
        attachments    = $atts
    }
}

# ---------------------------------------------------------------- parts, resume, disk space

$utf8 = New-Object System.Text.UTF8Encoding $false
$started = Get-Date
$script:runId = "OUTLOOK-" + $started.ToString("yyyy-MM-dd_HH-mm-ss")
$LogDir = Join-Path (Split-Path $OutRoot -Parent) "logs"
$IdsFile = Join-Path $OutRoot "exported-ids.txt"
$script:messages = New-Object System.Collections.ArrayList
$script:partNo = 0; $script:total = 0; $script:threads = @{}; $script:attSaved = 0; $script:AttFailed = 0
$script:noBody = 0; $script:retried = 0
$script:failures = New-Object System.Collections.ArrayList
$script:searched = @()

function New-Part {
    $script:partNo++
    $script:RunDir = Join-Path $OutRoot ("{0}-{1:D3}" -f $script:runId, $script:partNo)
    $script:AttDir = Join-Path $script:RunDir "attachments"
    New-Item -ItemType Directory -Force $script:AttDir | Out-Null
}

function Save-Part {
    if ($script:messages.Count -eq 0) { return }
    $acq = [ordered]@{
        source = "outlook"; acquisitionMethod = "Outlook COM v3"
        scope = $(if ($Selected) { "selected emails" } elseif ($Complete) { "complete: all folders, all dates" } else { "last $DaysBack days" })
        acquiredAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        part = $script:partNo; messages = $script:messages.ToArray()
    }
    $saved = @($script:messages | ForEach-Object { $_.attachments } | Where-Object { $_.acquired }).Count
    $man = [ordered]@{
        runId = (Split-Path $script:RunDir -Leaf); source = "outlook"; initiatedBy = $env:USERNAME
        part = $script:partNo; messageCount = $script:messages.Count; attachmentsSaved = $saved
        complete = [bool]$Complete; daysBack = $DaysBack; maxAttachmentSizeMB = $MaxAttachmentMB
        foldersSearched = $script:searched
    }
    # acquisition.json is written last and atomically: a part without it is incomplete
    $tmp = Join-Path $script:RunDir "acquisition.json.tmp"
    [System.IO.File]::WriteAllText((Join-Path $script:RunDir "manifest.json"), ($man | ConvertTo-Json -Depth 4), $utf8)
    [System.IO.File]::WriteAllText($tmp, ($acq | ConvertTo-Json -Depth 8), $utf8)
    Move-Item -Force $tmp (Join-Path $script:RunDir "acquisition.json")
    $ids = foreach ($x in $script:messages) { $x.entryId; if ($x.messageId) { "mid:" + $x.messageId } }
    if (-not $Selected) { [System.IO.File]::AppendAllText($IdsFile, (($ids | Where-Object { $_ }) -join "`r`n") + "`r`n", $utf8) }
    $script:attSaved += $saved
    Write-Host ("  part {0} saved: {1} items ({2:N0} so far)" -f $script:partNo, $script:messages.Count, $script:total)
    $script:messages.Clear()
    New-Part
}

function Test-DiskFull {
    try {
        $free = (Get-PSDrive -Name ((Resolve-Path $OutRoot).Path.Substring(0, 1)) -ErrorAction Stop).Free / 1GB
        return ($free -lt $MinFreeGB)
    } catch { return $false }
}

# ---------------------------------------------------------------- connection to Outlook
# "RPC server unavailable" and friends: Outlook closed, restarted, or stopped answering (a very
# large folder, a sleeping laptop, a network drop to Exchange). Instead of logging every remaining
# item as failed, wait, reconnect, find the same folder again and continue from the same item.
$ConnLost = '800706BA|800706BE|80010108|80010001|8001010A|RPC'
$script:ReconnectWaits = @(10, 30, 60)       # seconds before each reconnection attempt
$script:reconnects = 0
function Test-ConnLost($err) { return ("$($err.Exception.Message)" -match $ConnLost) }
function Connect-Outlook {
    if ($script:ConnectHook) { return (& $script:ConnectHook) }     # tests
    return (New-Object -ComObject Outlook.Application)
}
function Restore-Folder([string]$fid, [string]$sid, [string]$path) {
    foreach ($w in $script:ReconnectWaits) {
        Write-Host "    Outlook stopped responding in $path; waiting $w s, then reconnecting ..."
        Start-Sleep -Seconds $w
        try {
            $script:outlook = Connect-Outlook
            $f = $script:outlook.GetNamespace("MAPI").GetFolderFromID($fid, $sid)
            if ($f) { $script:reconnects++; Write-Host "    reconnected: continuing $path"; return $f }
        } catch {}
    }
    return $null
}

# ---------------------------------------------------------------- export

$script:exported = @{}
$script:syncIssues = 0; $script:currentRow = $null; $script:lostAt = ""
function Export-Folder($folder, [string]$label, [string]$path) {
    # One row of the reconciliation: Outlook's own count against what happened to each item.
    $row = [ordered]@{ folder = $path; items = 0; exported = 0; alreadyExported = 0; olderThanLimit = 0
                       failed = 0; notExportedCap = 0; notReached = 0 }
    $script:currentRow = $row          # kept if the export stops inside this folder
    $fid = [string](P { $folder.EntryID }); $sid = [string](P { $folder.StoreID })
    if (-not $sid) { $sid = [string](P { $folder.Store.StoreID }) }
    $items = $folder.Items
    $row.items = [int](P { $items.Count })
    if ($DaysBack -gt 0) {
        $field = $(if ($label -eq "SentMail") { "[SentOn]" } else { "[ReceivedTime]" })
        try { $items.Sort($field, $true) } catch {}
        $since = (Get-Date).AddDays(-$DaysBack)
    }
    $t0 = Get-Date
    for ($i = 1; $i -le $row.items; $i++) {
        $m = $null
        try { $m = Get-At $items $i } catch {
            if ((Test-ConnLost $_) -and -not $Selected -and $fid) {
                $nf = Restore-Folder $fid $sid $path
                if ($nf) {
                    $folder = $nf; $items = $folder.Items
                    if ($DaysBack -gt 0) { try { $items.Sort($field, $true) } catch {} }
                    try { $m = Get-At $items $i } catch { $m = $null }
                }
                if (-not $m) {
                    # Outlook did not come back: stop here; the rest is "not reached", not "failed"
                    $row.notReached = $row.items - $i + 1
                    $script:lostAt = "$path, item $i of $($row.items)"
                    throw "OUTLOOKLOST"
                }
            } else {
                $row.failed++
                [void]$script:failures.Add("$path`titem $i of $($row.items) could not be opened: $($_.Exception.Message)`t")
                continue
            }
        }
        if ($i % 250 -eq 0 -and $row.items -gt 0) {
            $rate = $i / [math]::Max(1, ((Get-Date) - $t0).TotalSeconds)
            Write-Host ("    {0}: {1:N0}/{2:N0} ({3:N1} items/s, ~{4:N0} min left in this folder)" -f `
                $path, $i, $row.items, $rate, (($row.items - $i) / [math]::Max($rate, 0.01) / 60))
        }
        if ($MaxMessages -gt 0 -and $script:total -ge $MaxMessages) { $row.notExportedCap = $row.items - $i + 1; break }
        if (-not $Complete -and -not $Selected -and (P { $m.Class }) -ne $olMail) { $row.failed++; [void]$script:failures.Add("$path`tnot an email (type $(P { $m.MessageClass }))`t$(P { $m.Subject })"); continue }
        if ($DaysBack -gt 0) {
            $when = $(if ($label -eq "SentMail") { P { $m.SentOn } } else { P { $m.ReceivedTime } })
            if ($when -and ([datetime]$when) -lt $since) { $row.olderThanLimit = $row.items - $i + 1; break }
        }
        $id = [string](P { $m.EntryID })
        if ($id -and $script:exported.ContainsKey($id)) { $row.alreadyExported++; continue }
        # the same email in the mailbox and an archive has a different EntryID but the same Message-ID
        $mid = [string](P { $m.PropertyAccessor.GetProperty($PR_INTERNET_MESSAGE_ID) })
        if ($mid -and $script:exported.ContainsKey("mid:" + $mid)) { $row.alreadyExported++; if ($id) { $script:exported[$id] = $true }; continue }
        try {
            try { $rec = Convert-Item $m $label $path }
            catch {
                if ((Test-ConnLost $_) -and $id -and -not $Selected) {
                    $nf = Restore-Folder $fid $sid $path
                    if (-not $nf) { $row.notReached = $row.items - $i + 1; $script:lostAt = "$path, item $i of $($row.items)"; throw "OUTLOOKLOST" }
                    $folder = $nf; $items = $folder.Items
                    if ($DaysBack -gt 0) { try { $items.Sort($field, $true) } catch {} }
                    $m = $script:outlook.GetNamespace("MAPI").GetItemFromID($id, $sid)
                } else { Start-Sleep -Seconds 2 }
                $script:retried++; $rec = Convert-Item $m $label $path     # one retry: transient Outlook errors
            }
            [void]$script:messages.Add($rec)
            if ($id) { $script:exported[$id] = $true }
            if ($mid) { $script:exported["mid:" + $mid] = $true }
            $script:threads[$rec.conversationId] = $true
            $script:total++; $row.exported++
        } catch {
            if ("$($_.Exception.Message)" -eq "OUTLOOKLOST") { throw }
            $row.failed++
            [void]$script:failures.Add("$path`t$($_.Exception.Message)`t$(P { $m.Subject })")
        }
        if ($script:messages.Count -ge $PartSize) {
            Save-Part
            if (Test-DiskFull) { $row.notReached = $row.items - $i; throw "DISKFULL" }
        }
    }
    $script:currentRow = $null
    return $row
}

function Get-Targets($ns) {
    $targets = @()
    $roots = @()
    if ($Stores.Count) {
        $all = @(); $sc = $null; try { $sc = $ns.Stores } catch {}; $n = Get-N $sc     # direct: a function return would enumerate it
        for ($k = 1; $k -le $n; $k++) {
            try { $all += ,(Get-At $sc $k) } catch { [void]$script:unreadable.Add("mailbox/data file #$k in Outlook's list: $($_.Exception.Message)") }
        }
        foreach ($st in $all) {
            if ($Stores -contains [string](P { $st.DisplayName })) {
                $root = P { $st.GetRootFolder() }
                if ($root) { $roots += ,$root } else { [void]$script:unreadable.Add("$(P { $st.DisplayName }): its folders could not be opened") }
            }
        }
        $missing = @($Stores | Where-Object { $nm = $_; -not ($all | Where-Object { (P { $_.DisplayName }) -eq $nm }) })
        if ($missing) { Write-Host "Not found in Outlook (not exported): $($missing -join ', ')" }
    } else {
        $explorer = P { $outlook.ActiveExplorer() }
        $roots = @($(if ($explorer) { P { $explorer.CurrentFolder } } else { $null }))
    }
    foreach ($cur in $roots) {
      if ($cur) {
        $sentId = P { $cur.Store.GetDefaultFolder(5).EntryID }
        $syncId = P { $cur.Store.GetDefaultFolder(20).EntryID }      # olFolderSyncIssues
        $walk = New-Object System.Collections.Stack
        $walk.Push(@($cur, [string](P { $cur.Name })))
        while ($walk.Count -gt 0) {
            $pair = $walk.Pop(); $f = $pair[0]; $path = $pair[1]
            # Outlook's own sync-problem logs ("Sync Issues": conflicts, local and server failures):
            # not the person's mail, so not exported, but counted, because they show the mailbox on
            # this laptop may not match the server
            $fname = [string](P { $f.Name })
            if (($syncId -and (P { $f.EntryID }) -eq $syncId) -or $fname -match '^(Sync Issues|Probl.mes de synchronisation)') {
                $n = [int](P { $f.Items.Count }); $subs = $null; try { $subs = $f.Folders } catch {}
                for ($k = 1; $k -le (Get-N $subs); $k++) { try { $n += [int](P { (Get-At $subs $k).Items.Count }) } catch {} }
                $script:syncIssues += $n
                continue
            }
            $isMail = ((P { $f.DefaultItemType }) -eq 0)
            $label = $(if ($sentId -and (P { $f.EntryID }) -eq $sentId) { "SentMail" } else { "Inbox" })
            if ($isMail) { $targets += ,@($f, $label, $path) }
            elseif ($Complete) { $script:nonMail += ,@($path, [int](P { $f.Items.Count })) }
            if ($IncludeSubfolders) {
                $subs = $null; try { $subs = $f.Folders } catch {}; $nsub = Get-N $subs     # direct, never through P
                for ($k = 1; $k -le $nsub; $k++) {
                    try { $s = Get-At $subs $k; $walk.Push(@($s, "$path\$(P { $s.Name })")) }
                    catch { [void]$script:unreadable.Add("$path (subfolder $k of $nsub): $($_.Exception.Message)") }
                }
            }
        }
        if (-not $IncludeSubfolders) {
            $sent = P { $cur.Store.GetDefaultFolder(5) }
            if ($sent) { $targets += ,@($sent, "SentMail", [string](P { $sent.Name })) }
        }
      }
    }
    if ($targets.Count -eq 0) {
        foreach ($f in $Folders) { $d = $ns.GetDefaultFolder($FolderIds[$f]); $targets += ,@($d, $f, [string]$d.Name) }
    }
    return ,$targets
}

function Invoke-Export {
    New-Item -ItemType Directory -Force $OutRoot, $LogDir | Out-Null
    if (-not $Selected -and (Test-Path $IdsFile)) {
        foreach ($l in [System.IO.File]::ReadAllLines($IdsFile)) { if ($l) { $script:exported[$l] = $true } }
        $n = @($script:exported.Keys | Where-Object { -not $_.StartsWith("mid:") }).Count; if ($n) { Write-Host "Resuming: $n items already exported earlier are not exported again." }
    }
    # never reuse an existing export folder (two runs in the same second would overwrite each other,
    # and the one-click summary recognises new work by a new folder)
    while (Test-Path (Join-Path $OutRoot ("{0}-001" -f $script:runId))) {
        Start-Sleep -Milliseconds 1100
        $script:runId = "OUTLOOK-" + (Get-Date).ToString("yyyy-MM-dd_HH-mm-ss")
    }
    New-Part
    if (-not $script:outlook) { $script:outlook = New-Object -ComObject Outlook.Application }
    $ns = $outlook.GetNamespace("MAPI")
    $rows = @(); $script:nonMail = @(); $stopped = $null

    try {
        if ($Selected) {
            $explorer = $outlook.ActiveExplorer()
            if ($null -eq $explorer -or $explorer.Selection.Count -eq 0) {
                throw "No emails are selected in Outlook. Select them first, then run again."
            }
            $script:searched = @("Selection in Outlook")
            $sel = [pscustomobject]@{ Items = $explorer.Selection }
            $rows += Export-Folder $sel "Selection" "Selection in Outlook"
        } else {
            $targets = Get-Targets $ns
            $script:searched = @($targets | ForEach-Object { $_[2] })
            Write-Host "Folders to export: $($targets.Count)"
            foreach ($t in $targets) {
                Write-Host "  $($t[2]) ..."
                $rows += Export-Folder $t[0] $t[1] $t[2]
            }
        }
    } catch {
        $msg = "$($_.Exception.Message)"
        if ($msg -eq "DISKFULL") { $stopped = "disk nearly full (under $MinFreeGB GB free)"; $again = "free some space and run again to continue" }
        elseif ($msg -eq "OUTLOOKLOST") { $stopped = "the connection to Outlook was lost at $($script:lostAt) and did not come back after $($script:ReconnectWaits.Count) attempts"
                                          $again = "check that classic Outlook is open and responding, keep the laptop awake and on power, then run again to continue" }
        else { throw }
        if ($script:currentRow) { $rows += $script:currentRow; $script:currentRow = $null }
    }
    Save-Part
    if (Test-Path $script:RunDir) {        # the empty part opened after the last save
        if (-not (Get-ChildItem $script:RunDir -Recurse -File)) { Remove-Item -Recurse -Force $script:RunDir; $script:partNo-- }
    }

    # ---------------------------------------------------------------- reconciliation
    $stamp = $started.ToString("yyyyMMdd-HHmm")
    $csv = Join-Path $LogDir "mail-folders-$stamp.csv"
    $rows | ForEach-Object { [pscustomobject]$_ } | Export-Csv -Path $csv -NoTypeInformation -Encoding UTF8
    if ($script:failures.Count -or $script:unreadable.Count) {
        [System.IO.File]::WriteAllText((Join-Path $LogDir "mail-failed-$stamp.txt"),
            "folder`treason`tsubject`r`n" + (@($script:failures) + @($script:unreadable | ForEach-Object { "(not readable)`t$_`t" }) -join "`r`n"), $utf8)
    }
    $sum = @{ items = 0; exported = 0; already = 0; older = 0; failed = 0; cap = 0; notReached = 0 }
    foreach ($r in $rows) {
        $sum.items += $r.items; $sum.exported += $r.exported; $sum.already += $r.alreadyExported
        $sum.older += $r.olderThanLimit; $sum.failed += $r.failed; $sum.cap += $r.notExportedCap
        $sum.notReached += [int]$r.notReached
    }
    $accounted = $sum.exported + $sum.already + $sum.older + $sum.failed + $sum.cap + $sum.notReached

    Write-Host ""
    Write-Host ("Folders: {0} | items in Outlook: {1:N0} | exported now: {2:N0} | already exported: {3:N0} | failed: {4:N0}" -f `
        $rows.Count, $sum.items, $sum.exported, $sum.already, $sum.failed)
    if ($sum.older -or $sum.cap) { Write-Host ("Not exported by choice: {0:N0} older than {1} days, {2:N0} over the message cap." -f $sum.older, $DaysBack, $sum.cap) }
    if ($script:nonMail.Count) {
        Write-Host ("Not email folders (calendar, contacts, tasks...), listed but not exported: " +
            (($script:nonMail | ForEach-Object { "$($_[0]) ($($_[1]))" }) -join "; "))
    }
    if ($script:AttFailed) { Write-Host "Attachments that could not be saved: $($script:AttFailed) (listed in each item's record)." }
    if ($script:unreadable.Count) {
        Write-Host "COULD NOT BE READ by Outlook (not exported):"
        $script:unreadable | ForEach-Object { Write-Host "  - $_" }
    }
    if ($script:noBody) { Write-Host "Items exported with no readable body (often encrypted or rights-protected): $($script:noBody)." }
    if ($script:retried) { Write-Host "Items that needed a second try: $($script:retried)." }
    if ($script:reconnects) { Write-Host "Outlook stopped responding and was reconnected $($script:reconnects) time(s); the export continued where it was." }
    if ($sum.notReached) { Write-Host ("Not reached yet: {0:N0} item(s); a new run continues from where this one stopped." -f $sum.notReached) }
    if ($script:syncIssues) {
        Write-Host ("Outlook's sync-problem folder holds {0:N0} item(s) (not exported: Outlook's own logs). Outlook had trouble" -f $script:syncIssues)
        Write-Host "  syncing with the server, so this laptop may not hold the whole mailbox. In Outlook: right-click a folder >"
        Write-Host "  Properties > Synchronization, and compare 'Server folder contains' with 'Offline folder contains'."
    }
    if ($stopped) {
        Write-Host "STOPPED: $stopped. Everything saved so far is kept; $again."
    } elseif ($script:unreadable.Count) {
        Write-Host "ACCOUNTED FOR WITH EXCEPTIONS: $($script:unreadable.Count) mailbox(es)/folder(s) could not be read (listed above), $($sum.failed) item(s) failed."
    } elseif ($accounted -ne $sum.items) {
        Write-Host ("CHECK: Outlook reported {0:N0} items but {1:N0} were accounted for (items may have arrived or moved during the export). Run again to pick up any missing." -f $sum.items, $accounted)
    } elseif ($sum.failed -eq 0 -and $sum.older -eq 0 -and $sum.cap -eq 0) {
        Write-Host "ALL ITEMS ACCOUNTED FOR: every item in every email folder was exported (now or in an earlier run)."
    } else {
        Write-Host "ACCOUNTED FOR WITH EXCEPTIONS: $($sum.failed) item(s) failed (listed in mail-failed-$stamp.txt)."
    }
    Write-Host "Per-folder counts: $csv"
    Write-Host ""
    Write-Host "$($script:total) messages in $($script:threads.Count) threads, $($script:attSaved) attachments saved, $($sum.failed) skipped."
    Write-Host "Written to $OutRoot ($($script:partNo) part folder(s) named $($script:runId)-NNN)"
    Write-Host "Next: run 'Embed new emails' from the menu (or embed.py --dry-run)."
}

if (-not $env:OA_NO_MAIN) { Invoke-Export }