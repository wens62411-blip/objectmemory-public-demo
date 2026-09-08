#Requires -Version 7.0
param([switch]$Apply, [switch]$Final, [switch]$Release, [switch]$VerifyGuards)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($Final -and $Release) { throw 'Final and Release are independent cleanup scopes; choose only one' }

# Exact exited fixtures from this audit only (including explicitly recorded
# interrupted Release runs; exiting never implies a passing test result).
# No wildcard, model, media root,
# dependency, current build or active root-suite directory is eligible.
$project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$temporary = [IO.Path]::GetFullPath((Join-Path $project 'data/temporary'))
$names = @(
    'public-phone-pipeline-first', 'public-phone-pipeline-authorized',
    'public-phone-pipeline-two-view', 'public-phone-pipeline-two-view-fixed',
    'public-phone-pipeline-final', 'public-phone-pipeline-browser',
    'public-phone-pipeline-v2', 'public-phone-browser-navigation-final',
    'public-phone-source-evidence-final'
)
$targets = @($names | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $temporary $_)) })
$dryRunPath = Join-Path $project 'data/verification/phone-public-test-cleanup-dry-run.json'
$resultPath = Join-Path $project 'data/verification/phone-public-test-cleanup.json'
if ($Final) {
    $names = @('phone-browser-replay-fixed', 'phone-full-backend-20260906',
        'phone-full-final-20260906', 'phone-live-box-20260906',
        'phone-shape-bridge-green', 'phone-shape-engine-red',
        'profile-upgrade-green', 'profile-upgrade-red', 'profile-upgrade-red-open',
        'registration-shape-guards-red', 'registration-shape-guards-red-authorized',
        'registration-shape-guards-green')
    $targets = @($names | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $temporary $_)) })
    $cachePaths = @('scripts/__pycache__', 'services/vision/__pycache__',
        'services/vision/trackers/__pycache__', 'services/vision/tests/__pycache__',
        'services/vision/events/__pycache__', 'services/vision/detectors/__pycache__',
        'services/vision/camera_sources/__pycache__', 'apps/api/__pycache__',
        'apps/api/tests/__pycache__', 'apps/api/app/__pycache__',
        'pytest-cache-files-3uoc8aep', 'pytest-cache-files-6010m2kn')
    $targets += @($cachePaths | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $project $_)) })
    $dryRunPath = Join-Path $project 'data/verification/phone-final-cache-cleanup-dry-run.json'
    $resultPath = Join-Path $project 'data/verification/phone-final-cache-cleanup.json'
}
if ($Release) {
    # Individually named, exited test runs only. Never include live service data,
    # Python bytecode caches being recreated by the ongoing REAL camera soak,
    # user reference images, models, dependency folders or the current web build.
    $names = @('phone-final-regression-20260906', 'phone-release-regression-20260906',
        'phone-release-final', 'phone-shape-http-20260906', 'phone-shape-http-fixed',
        'phone-final-browser', 'phone-settings-red', 'phone-settings-green',
        'phone-settings-final', 'phone-paths-red', 'phone-paths-green',
        'phone-shape-full-final')
    $targets = @($names | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $temporary $_)) })
    $dryRunPath = Join-Path $project 'data/verification/phone-release-cache-cleanup-dry-run.json'
    $resultPath = Join-Path $project 'data/verification/phone-release-cache-cleanup.json'
}

function Assert-NormalPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($project.TrimEnd('\')+'\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escaped the project: $full"
    }
    # Validate every existing ancestor from the drive root before inspecting its
    # children. Resolve-Path alone does not reliably reject Windows junctions.
    $drive = [IO.Path]::GetPathRoot($full)
    $cursor = $drive
    foreach ($part in $full.Substring($drive.Length).Split('\', [StringSplitOptions]::RemoveEmptyEntries)) {
        $cursor = Join-Path $cursor $part
        $entry = Get-Item -LiteralPath $cursor -Force
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing symlink/junction/reparse ancestor or entry: $cursor"
        }
    }
    $resolved = (Resolve-Path -LiteralPath $full).ProviderPath
    if (-not $resolved.Equals($full, [StringComparison]::OrdinalIgnoreCase)) { throw "Unexpected resolved path: $resolved" }
    return $resolved
}

function Assert-Inactive {
    $active = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='node.exe'" |
        Where-Object { $command = [string]$_.CommandLine; @($names | Where-Object { $command.Contains($_, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0 })
    if ($active.Count -gt 0) { throw ('A scoped test process is still active: '+(($active | ForEach-Object ProcessId) -join ',')) }
}

function Scan-Target([string]$Target) {
    if ($Target -notin $targets) { throw 'Target outside exact allowlist' }
    $null = Assert-NormalPath $temporary
    if (-not (Test-Path -LiteralPath $Target)) { return @() }
    $null = Assert-NormalPath $Target
    if (-not (Get-Item -LiteralPath $Target -Force).PSIsContainer) { throw 'Target is not a regular directory' }
    $pending = [Collections.Generic.Stack[string]]::new()
    $rows = [Collections.Generic.List[object]]::new()
    $pending.Push($Target)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        $null = Assert-NormalPath $directory
        $rows.Add([pscustomobject]@{path=$directory; target=$Target; kind='directory'; bytes=0L; sha256=$null; modified_ticks=0L})
        # Never use recursive enumeration or follow a link.
        foreach ($entry in @(Get-ChildItem -LiteralPath $directory -Force)) {
            if (($Final -or $Release) -and ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                # Keep the link itself and its containing directories. Never
                # enumerate, archive, hash or delete its target.
                $rows.Add([pscustomobject]@{path=$entry.FullName;target=$Target;kind='protected_link';bytes=0L;sha256=$null;modified_ticks=0L})
                continue
            }
            $null = Assert-NormalPath $entry.FullName
            if ($entry.PSIsContainer) { $pending.Push($entry.FullName) }
            else {
                $rows.Add([pscustomobject]@{path=$entry.FullName; target=$Target; kind='file'; bytes=[long]$entry.Length;
                    sha256=(Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash;
                    modified_ticks=$entry.LastWriteTimeUtc.Ticks})
            }
        }
    }
    return $rows.ToArray()
}

function Total($Rows) {
    $files = @($Rows | Where-Object kind -eq 'file')
    $bytes = 0L
    foreach ($file in $files) { $bytes += [long]$file.bytes }
    return @{files=$files.Count; bytes=$bytes; directories=@($Rows | Where-Object kind -eq 'directory').Count}
}

function Write-Json([string]$Path, $Value) {
    $null = Assert-NormalPath (Split-Path -Parent $Path)
    if ((Test-Path -LiteralPath $Path) -or (Test-Path -LiteralPath ($Path+'.part'))) { throw "Existing report preserved, refusing overwrite: $Path" }
    [IO.File]::WriteAllText($Path+'.part', ($Value | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath ($Path+'.part') -Destination $Path
}

function Preserve-DiagnosticEvidence($Entries) {
    # Shared by Final apply and Release preview/apply. Never copy databases,
    # configuration, tokens, uploads or raw source-video fixtures.
    $archiveName = if ($Release) { 'phone-release-preserved-test-evidence' } else { 'phone-preserved-test-evidence' }
    $archive = Join-Path $project ('data/verification/'+$archiveName)
    $null = Assert-NormalPath (Split-Path -Parent $archive)
    if (-not (Test-Path -LiteralPath $archive)) { $null = New-Item -ItemType Directory -Path $archive }
    $null = Assert-NormalPath $archive
    $preservedRows = [Collections.Generic.List[object]]::new()
    foreach ($row in @($Entries | Where-Object { $_.kind -eq 'file' -and
        $_.target.StartsWith($temporary+'\',[StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($_.path) -match '(result|evidence|browser|http|posture|actions|release|metrics).*\.json$|^backend.*\.log$|^public-observed\.jpg$|^actual-source-\d+\.jpg$' })) {
        $null = Assert-NormalPath $row.path
        if ((Get-FileHash -LiteralPath $row.path -Algorithm SHA256).Hash -ne $row.sha256) { throw 'Source evidence changed since scan' }
        # A flat hash-prefixed archive avoids long Windows test paths.
        $basename = $row.sha256.Substring(0,12).ToLowerInvariant()+'-'+[IO.Path]::GetFileName($row.path)
        $destination = [IO.Path]::GetFullPath((Join-Path $archive $basename))
        if (-not $destination.StartsWith($archive+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Archive path escaped' }
        if (-not (Test-Path -LiteralPath $destination)) { Copy-Item -LiteralPath $row.path -Destination $destination }
        $null = Assert-NormalPath $destination
        if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -ne $row.sha256) { throw 'Preserved evidence hash differs' }
        $preservedRows.Add([pscustomobject]@{source=$row.path;path=$destination;bytes=$row.bytes;sha256=$row.sha256})
    }
    return $preservedRows.ToArray()
}

function Assert-ReleaseEvidence {
    if (-not $Release) { return }
    $manifestPath = Join-Path $project 'data/verification/phone-final-public-browser/archive-manifest.json'
    $null = Assert-NormalPath $manifestPath
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.status -ne 'copied_and_sha256_verified' -or $manifest.runtime_mode -ne 'TEST' -or
        $manifest.source_type -ne 'video_file' -or $manifest.physical_camera -or @($manifest.files).Count -ne 14) { throw 'Final public browser evidence archive is incomplete' }
    $evidenceRoot = Join-Path $project 'data/verification/phone-final-public-browser'
    foreach ($row in $manifest.files) {
        if ($row.case -notin @('single','two')) { throw 'Unexpected public evidence case' }
        $path = [IO.Path]::GetFullPath((Join-Path $evidenceRoot ($row.case+'/'+$row.relative)))
        if (-not $path.StartsWith($evidenceRoot+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Archived evidence path escaped' }
        $null = Assert-NormalPath $path
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $row.sha256 -or
            (Get-Item -LiteralPath $path).Length -ne $row.bytes) { throw 'Final public evidence hash or length changed' }
    }
    $interruptedPath = Join-Path $project 'data/verification/phone-shape-full-final-interrupted.json'
    $null = Assert-NormalPath $interruptedPath
    $interrupted = Get-Content -LiteralPath $interruptedPath -Raw | ConvertFrom-Json
    if ($interrupted.status -ne 'INTERRUPTED_NO_FINAL_RESULT' -or $interrupted.counts_as_passed) { throw 'Interrupted run must not be recorded as passing' }
    if (Test-Path -LiteralPath (Join-Path $project 'data/verification/pytest-phone-shape-full-final.xml')) { throw 'Interrupted result state changed; re-audit before cleanup' }
}

if ($VerifyGuards) {
    $checked = 0
    foreach ($forbidden in @($project, $temporary, 'C:\Windows\System32',
            (Join-Path $project 'data/database'), (Join-Path $project 'data/registered-items'),
            (Join-Path $project '.venv'), (Join-Path $project 'apps/web/node_modules'))) {
        $rejected = $false
        try { $null = Scan-Target $forbidden } catch { $rejected = $true }
        if (-not $rejected) { throw "Safety regression: broad/protected target accepted: $forbidden" }
        $checked++
    }
    [pscustomobject]@{guard_cases=$checked;passed=$true;files_deleted=0;links_followed=$false} | ConvertTo-Json
    exit 0
}
Assert-Inactive
Assert-ReleaseEvidence
if (-not $Apply) {
    $entries = [Collections.Generic.List[object]]::new()
    $summary = [Collections.Generic.List[object]]::new()
    $errors = [Collections.Generic.List[object]]::new()
    foreach ($target in $targets) {
        try {
            $rows = @(Scan-Target $target)
            foreach ($row in $rows) { $entries.Add($row) }
            $summary.Add([pscustomobject]@{path=$target; status=$(if($rows.Count){'planned'}else{'missing'}); before=(Total $rows)})
        } catch { $errors.Add([pscustomobject]@{path=$target; error=$_.Exception.Message}) }
    }
    # Release review saves unique diagnostic evidence before proposing deletion.
    # This is non-destructive: only new hash-verified copies inside verification.
    $releaseEvidence = @()
    if ($Release -and $errors.Count -eq 0) { $releaseEvidence = @(Preserve-DiagnosticEvidence $entries.ToArray()) }
    $plan = [ordered]@{schema='public_phone_exact_cleanup_v1';final_scope=[bool]$Final;release_scope=[bool]$Release;dry_run=$true;created_at=[DateTime]::UtcNow.ToString('o');
        project=$project; temporary_root=$temporary; targets=$summary.ToArray(); entries=$entries.ToArray();
        before=(Total $entries.ToArray()); errors=$errors.ToArray(); links_followed=$false; recursive_remove=$false;
        protected=@('data/verification (all historical failure and final evidence)', 'data/test-assets (public input videos)',
                    'data/database', 'data/real-media', 'data/registered-items', 'data/models', '.venv', 'node_modules',
                    'all directories outside the exact allowlist; all links and their targets');
        preserved_evidence=$releaseEvidence}
    Write-Json $dryRunPath $plan
    [pscustomobject]@{dry_run=$true;before=$plan.before;errors=$errors.Count;report=$dryRunPath} | ConvertTo-Json -Depth 5
    if ($errors.Count) { exit 1 }
    exit 0
}

$null = Assert-NormalPath $dryRunPath
if (Test-Path -LiteralPath $resultPath) { throw 'Cleanup result already exists; refusing duplicate deletion run' }
$plan = Get-Content -LiteralPath $dryRunPath -Raw | ConvertFrom-Json
if ($plan.schema -ne 'public_phone_exact_cleanup_v1' -or $plan.project -ne $project -or @($plan.errors).Count) { throw 'Invalid or incomplete dry run' }
if ($Final -and (-not $plan.PSObject.Properties['final_scope'] -or -not $plan.final_scope)) { throw 'Final scope requires its own dry run' }
if ($Release -and (-not $plan.PSObject.Properties['release_scope'] -or -not $plan.release_scope)) { throw 'Release scope requires its own dry run' }
if (@($plan.targets).Count -ne $targets.Count -or @($plan.targets | Where-Object path -notin $targets).Count) { throw 'Dry-run target allowlist mismatch' }
$planHash = (Get-FileHash -LiteralPath $dryRunPath -Algorithm SHA256).Hash
# Re-scan every target before deleting any file. Added/replaced files, changed
# hashes/timestamps, active processes and links all invalidate the whole plan.
foreach ($target in $targets) {
    $current = @(Scan-Target $target | Sort-Object path)
    $planned = @($plan.entries | Where-Object target -eq $target | Sort-Object path)
    if (($current | ConvertTo-Json -Depth 5 -Compress) -cne ($planned | ConvertTo-Json -Depth 5 -Compress)) { throw "Target changed since dry run: $target" }
}
Assert-Inactive
$preserved = [Collections.Generic.List[object]]::new()
if ($Final -or $Release) {
    foreach ($row in @(Preserve-DiagnosticEvidence $plan.entries)) { $preserved.Add($row) }
}
$deleted = [Collections.Generic.List[object]]::new()
$errors = [Collections.Generic.List[object]]::new()
$ordered = @($plan.entries | Where-Object kind -eq 'file') + @($plan.entries | Where-Object kind -eq 'directory' | Sort-Object { $_.path.Length } -Descending)
foreach ($row in $ordered) {
    try {
        if ($row.target -notin $targets -or ($row.path -ne $row.target -and -not $row.path.StartsWith($row.target+'\',[StringComparison]::OrdinalIgnoreCase))) { throw 'Entry escaped its exact target' }
        $null = Assert-NormalPath $row.path
        $entry = Get-Item -LiteralPath $row.path -Force
        if ($row.kind -eq 'file') {
            if ($entry.PSIsContainer -or $entry.Length -ne $row.bytes -or $entry.LastWriteTimeUtc.Ticks -ne $row.modified_ticks -or
                (Get-FileHash -LiteralPath $row.path -Algorithm SHA256).Hash -ne $row.sha256) { throw 'File changed immediately before deletion' }
        } else {
            if (($Final -or $Release) -and $entry.PSIsContainer -and @(Get-ChildItem -LiteralPath $row.path -Force).Count) { continue }
            if (-not $entry.PSIsContainer -or @(Get-ChildItem -LiteralPath $row.path -Force).Count) { throw 'Only empty validated directories can be removed' }
        }
        Remove-Item -LiteralPath $row.path -Force -ErrorAction Stop
        $deleted.Add($row)
    } catch { $errors.Add([pscustomobject]@{path=$row.path;error=$_.Exception.Message}) }
}
$remaining = [Collections.Generic.List[object]]::new()
$afterTargets = @($targets | ForEach-Object { $rows=@(Scan-Target $_);foreach($row in $rows){$remaining.Add($row)};
    [pscustomobject]@{path=$_;exists=(Test-Path -LiteralPath $_);after=(Total $rows)} })
$result = [ordered]@{schema=$plan.schema;completed_at=[DateTime]::UtcNow.ToString('o');dry_run_report=$dryRunPath;dry_run_sha256=$planHash;
    exact_targets=$targets;before=$plan.before;deleted=(Total $deleted.ToArray());after=(Total $remaining.ToArray());
    targets_after=$afterTargets;errors=$errors.ToArray();protected=$plan.protected;links_followed=$false;recursive_remove=$false;
    preserved_evidence=$preserved.ToArray();
    note=$(if ($Release) { 'Only exact exited test fixtures are eligible, including one separately documented interrupted run; deletion does not imply tests passed. Preserved evidence bytes are separate; no model bytes counted here.' } else { 'Only exact completed fixtures/caches deleted. Preserved evidence bytes are separate; no model bytes counted here.' })}
Write-Json $resultPath $result
[pscustomobject]@{before=$result.before;deleted=$result.deleted;after=$result.after;errors=$errors.Count;report=$resultPath} | ConvertTo-Json -Depth 5
if ($errors.Count) { exit 1 }
