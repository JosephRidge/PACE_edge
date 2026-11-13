# mail_checks.ps1 - checks shown BEFORE an email export starts (loaded by extract_mail.ps1).
# They catch the three ways an export looked complete but was not, found on real laptops:
#   - Outlook keeps only the last N months on the laptop (Cached Exchange Mode): older mail is
#     invisible to any program reading Outlook, so it cannot be exported
#   - no archive open in Outlook: older mail often lives in an Online Archive or .pst files
#   - the person's own mailbox not selected (only an archive exported)

function Get-SyncWindowMonths {
    # "Mail to keep offline", in months (0 = all). A policy set by IT wins over the user's setting.
    # $null = not found here (newer Outlook may keep it per account): then the person checks it.
    foreach ($v in "16.0", "15.0") {
        foreach ($k in "HKCU:\Software\Policies\Microsoft\Office\$v\Outlook\Cached Mode",
                       "HKCU:\Software\Microsoft\Office\$v\Outlook\Cached Mode") {
            try { return [int](Get-ItemProperty -Path $k -Name SyncWindowSetting -ErrorAction Stop).SyncWindowSetting } catch {}
        }
    }
    return $null
}

function Get-ExportWarnings($stores, $months) {
    # lines to show before the choice; each store has Name, Kind, Cached
    $w = @()
    $cached = @($stores | Where-Object { $_.Cached })
    if ($months -gt 0) {
        $w += "WARNING: Outlook keeps only the last $months month(s) of mail on this laptop. Older mail is NOT"
        $w += "  visible to this export. To include it: File > Account Settings > Account Settings > your account >"
        $w += "  Change > 'Mail to keep offline' = All > Next; restart Outlook, wait until the bottom bar says"
        $w += "  'All folders are up to date', then run this again (it only adds the older mail)."
    } elseif ($cached.Count -and $null -eq $months) {
        $w += "CHECK: make sure Outlook keeps ALL your mail on this laptop: File > Account Settings > Account"
        $w += "  Settings > your account > Change > 'Mail to keep offline' should be 'All'. Otherwise older mail"
        $w += "  is not on this laptop and cannot be exported."
    }
    if (-not @($stores | Where-Object { $_.Kind -match 'archive|\.pst' -or $_.Name -match 'archive|archiv' }).Count) {
        $w += "NOTE: no archive is open in Outlook. If you have an Online Archive or old .pst files, open or"
        $w += "  expand them in Outlook first (File > Open & Export > Open Outlook Data File), then run this again."
    }
    return ,$w
}

function Test-OwnMailboxMissing($stores, $picked) {
    # true when the person's own mailbox is listed but not among the chosen ones
    $own = @($stores | Where-Object { $_.Kind -eq "mailbox" })
    if (-not $own.Count) { return $false }
    $names = @($picked | ForEach-Object { $_.Name })
    return (-not @($own | Where-Object { $names -contains $_.Name }).Count)
}