#Requires -Version 7.0
param([switch]$Apply, [switch]$FinalizePlan, [switch]$TestsStopped)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# One audited run's exact artifacts, never a wildcard or a general data cleaner.
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPrefix = $taskRoot.TrimEnd('\') + '\'
$relativeTargets = @(
    'data/temporary/preview-unit-approved',
    'data/temporary/preview-unit-initial',
    'data/temporary/preview-vision-related-approved',
    'data/temporary/preview-vision-related-final',
    'data/temporary/preview-vision-frozen',
    'data/temporary/pytest-usability-final',
    'data/temporary/pytest-usability-frozen-v2',
    'data/temporary/pytest-usability-release',
    'data/test-camera-usability-20260905',
    'data/test-camera-usability-authorized-20260905',
    'data/test-camera-usability-gate-20260905',
    'data/test-camera-usability-final-20260905',
    'data/test-camera-metadata-red-20260905',
    'data/test-camera-metadata-red-contract-20260905',
    'data/test-camera-metadata-green-20260905',
    'data/test-camera-cli-final-20260905',
    '.pytest_cache', 'pytest-cache-files-xxnyyo1f', 'pytest-cache-files-z5ltpmdq',
    'apps/api/__pycache__', 'apps/api/app/__pycache__', 'apps/api/tests/__pycache__',
    'scripts/__pycache__', 'services/vision/__pycache__',
    'services/vision/camera_sources/__pycache__', 'services/vision/detectors/__pycache__',
    'services/vision/events/__pycache__', 'services/vision/trackers/__pycache__'
)
$allowedTargets = @($relativeTargets | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $taskRoot $_)) })
$supersededReports = @('data/verification/pytest-usability-round1.xml', 'audit/usability/cleanup-dry-run-pre-release.json')
$allowedReports = @($supersededReports | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $taskRoot $_)) })
$dryRunPath = Join-Path $taskRoot 'audit/usability/cleanup-dry-run.json'
$resultPath = Join-Path $taskRoot 'audit/usability/cleanup-result.json'

function Assert-Confined([string]$Path, [bool]$AllowLeafLink = $false) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($taskPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Outside the exact project boundary: $full"
    }
    $ancestors = [Collections.Generic.List[string]]::new()
    $cursor = $taskRoot
    $ancestors.Add($cursor)
    foreach ($part in ($full.Substring($taskPrefix.Length) -split '[\\/]')) {
        $cursor = Join-Path $cursor $part
        $ancestors.Add($cursor)
    }
    # Walk outward from the trusted root: never even inspect a child through a
    # link before rejecting its reparse-point parent.
    foreach ($cursor in $ancestors) {
        $entry = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            if (-not ($AllowLeafLink -and $cursor -eq $full -and $entry.LinkType -in @('SymbolicLink', 'Junction'))) {
                throw "Refusing reparse point or reparse ancestor: $cursor"
            }
        }
    }
    return $full
}

function Get-TreePlan([string]$Target) {
    $null = Assert-Confined $Target
    if (-not (Get-Item -LiteralPath $Target -Force).PSIsContainer) { throw "Target is not a directory: $Target" }
    $pending = [Collections.Generic.Stack[string]]::new()
    $rows = [Collections.Generic.List[object]]::new()
    $pending.Push($Target)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        $null = Assert-Confined $directory
        $rows.Add([pscustomobject]@{ path=$directory; target=$Target; kind='directory'; bytes=0; sha256=$null; modified_ticks=0 })
        # No -Recurse: each child is checked before descending, never follow links.
        foreach ($entry in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            $null = Assert-Confined $entry.FullName $true
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                $rows.Add([pscustomobject]@{ path=$entry.FullName; target=$Target; kind='link'; bytes=0; sha256=$null; modified_ticks=$entry.LastWriteTimeUtc.Ticks })
            } elseif ($entry.PSIsContainer) {
                $pending.Push($entry.FullName)
            } else {
                $hash = (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash
                $rows.Add([pscustomobject]@{ path=$entry.FullName; target=$Target; kind='file'; bytes=$entry.Length; sha256=$hash; modified_ticks=$entry.LastWriteTimeUtc.Ticks })
            }
        }
    }
    return $rows.ToArray()
}

function Write-Report([string]$Path, $Value, [bool]$ReplaceUnappliedPlan = $false) {
    $null = Assert-Confined (Split-Path -Parent $Path)
    if (Test-Path -LiteralPath $Path) {
        if (-not ($ReplaceUnappliedPlan -and $Path -eq $dryRunPath -and -not (Test-Path -LiteralPath $resultPath))) {
            throw "Refusing to overwrite an existing audit report: $Path"
        }
        $null = Assert-Confined $Path
    }
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
}

$guarantees = @{
    exact_allowlist_only=$true; recursive_remove_used=$false; links_followed=$false
    dependencies_and_current_build_excluded=$true; physical_camera_or_service_commands_used=$false
    protected_roots=@('data/database','data/real-media','data/demo-media','data/backups','data/uploads',
        'data/logs','data/verification except exact superseded success report',
        'audit except these two cleanup reports and exact unapplied draft','apps/web/dist','.venv','apps/web/node_modules')
    exact_report_exceptions=$supersededReports
}

function Add-ReportPruning($Plan) {
    foreach ($path in $allowedReports) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        if ($path -in @($Plan.entries | ForEach-Object path)) { continue }
        $null = Assert-Confined $path
        $entry = Get-Item -LiteralPath $path -Force
        if ($entry.PSIsContainer) { throw 'Superseded report must be an ordinary file' }
        $Plan.entries += [pscustomobject]@{path=$path;target=$path;kind='file';bytes=$entry.Length;
            sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash;modified_ticks=$entry.LastWriteTimeUtc.Ticks}
        $Plan.targets += [pscustomobject]@{path=$path;status='planned_superseded_report';files=1;bytes=$entry.Length}
        $Plan.planned_files++; $Plan.planned_bytes += $entry.Length
        if ($path.EndsWith('.xml')) {
            [xml]$previousXml = Get-Content -LiteralPath $path -Raw
            $suite = $previousXml.testsuites.testsuite
            $Plan['superseded_success_round'] = @{tests=[int]$suite.tests;failures=[int]$suite.failures;
                errors=[int]$suite.errors;junit_seconds=[double]$suite.time;console_seconds=220.29;
                reason='Duplicate successful intermediate round; release 377 and previous 369 reports retained'}
        } else {
            $previousPlan = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            $Plan['superseded_preliminary_plan'] = @{planned_files=$previousPlan.planned_files;
                planned_bytes=$previousPlan.planned_bytes;errors=$previousPlan.errors;never_applied=$true;
                correction='Four empty directories hit StrictMode missing Sum; explicit zero-byte accumulation fixed and final dry run has zero scan errors'}
        }
    }
    $Plan.guarantees = $guarantees
}

if ($FinalizePlan) {
    if ($Apply) { throw 'FinalizePlan is read-only planning; do not combine it with Apply' }
    $null = Assert-Confined $dryRunPath
    $plan = Get-Content -LiteralPath $dryRunPath -Raw | ConvertFrom-Json -AsHashtable
    if ($plan.schema -ne 'usability_exact_cleanup_v1' -or $plan.root -ne $taskRoot) { throw 'Unrecognized cleanup plan' }
    Add-ReportPruning $plan
    Write-Report $dryRunPath $plan $true
    [pscustomobject]@{dry_run=$true;files=$plan.planned_files;bytes=$plan.planned_bytes;errors=@($plan.errors).Count;report=$dryRunPath} | ConvertTo-Json
    exit 0
}

if (-not $Apply) {
    $rows = [Collections.Generic.List[object]]::new()
    $targets = [Collections.Generic.List[object]]::new()
    $errors = [Collections.Generic.List[object]]::new()
    foreach ($target in $allowedTargets) {
        try {
            if (-not (Test-Path -LiteralPath $target)) {
                $targets.Add([pscustomobject]@{path=$target; status='missing'; files=0; bytes=0})
                continue
            }
            $entries = @(Get-TreePlan $target)
            foreach ($entry in $entries) { $rows.Add($entry) }
            $files = @($entries | Where-Object kind -eq 'file')
            $targetBytes = 0L
            foreach ($file in $files) { $targetBytes += [long]$file.bytes }
            $targets.Add([pscustomobject]@{path=$target; status='planned'; files=$files.Count; bytes=$targetBytes})
        } catch { $errors.Add([pscustomobject]@{path=$target; error=$_.Exception.Message}) }
    }
    $files = @($rows | Where-Object kind -eq 'file')
    $plannedBytes = 0L
    foreach ($file in $files) { $plannedBytes += [long]$file.bytes }
    $plan = [ordered]@{ schema='usability_exact_cleanup_v1'; dry_run=$true; created_at=[DateTime]::UtcNow.ToString('o')
        root=$taskRoot; targets=$targets.ToArray(); entries=$rows.ToArray(); planned_files=$files.Count
        planned_bytes=$plannedBytes; errors=$errors.ToArray(); guarantees=$guarantees }
    Add-ReportPruning $plan
    Write-Report $dryRunPath $plan
    [pscustomobject]@{dry_run=$true; files=$plan.planned_files; bytes=$plan.planned_bytes; errors=$errors.Count; report=$dryRunPath} | ConvertTo-Json
    exit 0
}

$null = Assert-Confined $dryRunPath
if (-not $TestsStopped) { throw 'Apply requires explicit confirmation that all scoped tests have ended' }
$activeTests = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -match '(?i)(?:-m\s+pytest|pytest\.exe)' })
if ($activeTests.Count -gt 0) { throw 'A Python pytest process is still active; refusing cleanup' }
if (Test-Path -LiteralPath $resultPath) { throw 'Cleanup result already exists; refusing another destructive run' }
$plan = Get-Content -LiteralPath $dryRunPath -Raw | ConvertFrom-Json
if ($plan.schema -ne 'usability_exact_cleanup_v1' -or $plan.root -ne $taskRoot) { throw 'Unrecognized cleanup plan' }
$planHash = (Get-FileHash -LiteralPath $dryRunPath -Algorithm SHA256).Hash
$errors = [Collections.Generic.List[object]]::new()
$deleted = [Collections.Generic.List[object]]::new()
$deletedFiles = 0; $deletedBytes = 0L; $deletedLinks = 0; $deletedDirectories = 0
# Unlink first, so independently authorized fixtures cannot turn those links
# dangling during validation. Then delete files and only EMPTY directories.
$ordered = @($plan.entries | Where-Object kind -eq 'link') + @($plan.entries | Where-Object kind -eq 'file') + @($plan.entries | Where-Object kind -eq 'directory' | Sort-Object { $_.path.Length } -Descending)
foreach ($row in $ordered) {
    try {
        $row.target = [IO.Path]::GetFullPath($row.target)
        $row.path = [IO.Path]::GetFullPath($row.path)
        if ($row.target -notin ($allowedTargets + $allowedReports)) { throw 'Target is not in the exact allowlist' }
        $prefix = $row.target.TrimEnd('\') + '\'
        if ($row.path -ne $row.target -and -not $row.path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Entry escaped its allowlisted target' }
        $null = Assert-Confined $row.path ($row.kind -eq 'link')
        $entry = Get-Item -LiteralPath $row.path -Force
        $isLink = ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        if ($row.kind -eq 'link') {
            if (-not $isLink -or $entry.LastWriteTimeUtc.Ticks -ne $row.modified_ticks) { throw 'Link changed since dry run' }
            Remove-Item -LiteralPath $row.path -Force -ErrorAction Stop
            $deletedLinks++
        } elseif ($row.kind -eq 'file') {
            if ($isLink -or $entry.PSIsContainer -or $entry.Length -ne $row.bytes -or $entry.LastWriteTimeUtc.Ticks -ne $row.modified_ticks) { throw 'File changed since dry run' }
            if ((Get-FileHash -LiteralPath $row.path -Algorithm SHA256).Hash -ne $row.sha256) { throw 'File hash changed since dry run' }
            Remove-Item -LiteralPath $row.path -Force -ErrorAction Stop
            $deletedFiles++; $deletedBytes += [long]$row.bytes
        } elseif ($row.kind -eq 'directory') {
            if ($isLink -or -not $entry.PSIsContainer) { throw 'Directory type changed since dry run' }
            if (@(Get-ChildItem -LiteralPath $row.path -Force).Count -ne 0) { throw 'Directory is not empty; unplanned or retained contents preserved' }
            # Directory.Delete(false) cannot implicitly recurse if a new child
            # appears after the emptiness check (Remove-Item may ask to recurse).
            [IO.Directory]::Delete($row.path, $false)
            $deletedDirectories++
        } else { throw 'Unknown entry type' }
        $deleted.Add([pscustomobject]@{path=$row.path;kind=$row.kind;bytes=$row.bytes})
    } catch { $errors.Add([pscustomobject]@{path=$row.path;error=$_.Exception.Message}) }
}
foreach ($scanError in @($plan.errors)) { $errors.Add($scanError) }
$result = [ordered]@{ schema='usability_exact_cleanup_v1'; dry_run=$false; completed_at=[DateTime]::UtcNow.ToString('o')
    dry_run_sha256=$planHash; success=($errors.Count -eq 0); deleted_files=$deletedFiles; deleted_bytes=$deletedBytes
    deleted_links=$deletedLinks; deleted_directories=$deletedDirectories; errors=$errors.ToArray()
    deleted=$deleted.ToArray(); guarantees=$guarantees; tests_stopped_confirmed=$TestsStopped.IsPresent
    superseded_success_round=$plan.superseded_success_round; superseded_preliminary_plan=$plan.superseded_preliminary_plan }
Write-Report $resultPath $result
[pscustomobject]@{success=$result.success; files=$deletedFiles; bytes=$deletedBytes; links=$deletedLinks; errors=$errors.Count; report=$resultPath} | ConvertTo-Json
if ($errors.Count -gt 0) { exit 1 }
