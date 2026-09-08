#Requires -Version 7.0
param([switch]$Apply,[string]$EvidenceDirectory='data/verification/core-vision')
$ErrorActionPreference='Stop'
$omRoot=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$omEvidence=[IO.Path]::GetFullPath((Join-Path $omRoot $EvidenceDirectory))
$omRows=[Collections.Generic.List[object]]::new()
$omPreserved=[Collections.Generic.List[object]]::new()
$omEnumeration=[IO.EnumerationOptions]::new()
$omEnumeration.RecurseSubdirectories=$false
$omEnumeration.IgnoreInaccessible=$false
$omEnumeration.AttributesToSkip=[IO.FileAttributes]0
$omEnumeration.ReturnSpecialDirectories=$false
$omEnumeration.BufferSize=65536
function Assert-OmNormalPath([string]$Path,[string]$Boundary=$omRoot,[switch]$AllowMissing) {
    $omFull=[IO.Path]::GetFullPath($Path)
    $omBase=[IO.Path]::GetFullPath($Boundary).TrimEnd('\')
    if(-not $omFull.Equals($omBase,[StringComparison]::OrdinalIgnoreCase) -and
       -not $omFull.StartsWith($omBase+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Path outside safety boundary'}
    # Include the project root and every drive ancestor, not only its children.
    $omDrive=[IO.Path]::GetPathRoot($omFull);$omCursor=$omDrive
    foreach($omPart in $omFull.Substring($omDrive.Length).Split('\',[StringSplitOptions]::RemoveEmptyEntries)) {
        $omCursor=Join-Path $omCursor $omPart
        try {$omEntry=Get-Item -LiteralPath $omCursor -Force -ErrorAction Stop}
        catch [System.Management.Automation.ItemNotFoundException] {if($AllowMissing){continue};throw}
        if($omEntry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw "Unsafe reparse ancestor or entry: $omCursor"}
    }
    return $omFull
}
function Get-OmSafeTree([string]$Target) {
    $null=Assert-OmNormalPath $Target
    $omTree=[Collections.Generic.List[object]]::new()
    $omStack=[Collections.Generic.Stack[IO.DirectoryInfo]]::new();$omStack.Push([IO.DirectoryInfo]::new($Target))
    while($omStack.Count){
        $omNode=$omStack.Pop();$omNode.Refresh()
        if($omNode.Attributes -band [IO.FileAttributes]::ReparsePoint){throw "Reparse point requires separate review: $($omNode.FullName)"}
        if(-not $omNode.Exists){throw 'Directory vanished during inventory'}
        $omTree.Add($omNode)
        # FileSystemInfo reuses metadata returned by this one-level enumeration.
        # Do not ask Get-Item again for every file, or recurse through a link.
        foreach($omChild in $omNode.EnumerateFileSystemInfos('*',$omEnumeration)) {
            if($omChild.Attributes -band [IO.FileAttributes]::ReparsePoint){throw "Reparse point requires separate review: $($omChild.FullName)"}
            if(-not $omChild.FullName.StartsWith($Target.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Tree entry escaped target'}
            if($omChild -is [IO.DirectoryInfo]){$omStack.Push($omChild)}else{$omTree.Add($omChild)}
        }
    }
    return ,$omTree
}
function Get-OmBytes([string]$Target) {
    # Accounting only: do not traverse links, including test security junctions.
    $null=Assert-OmNormalPath $Target
    $omTotal=0L;$omStack=[Collections.Generic.Stack[IO.DirectoryInfo]]::new();$omStack.Push([IO.DirectoryInfo]::new($Target))
    while($omStack.Count){
        $omDir=$omStack.Pop();$omDir.Refresh()
        if($omDir.Attributes -band [IO.FileAttributes]::ReparsePoint){continue}
        foreach($omNode in $omDir.EnumerateFileSystemInfos('*',$omEnumeration)){
            if($omNode.Attributes -band [IO.FileAttributes]::ReparsePoint){continue}
            if($omNode -is [IO.DirectoryInfo]){$omStack.Push($omNode)}else{$omTotal+=$omNode.Length}
        }
    }
    return $omTotal
}
function Get-OmMetadata($Tree) {
    $omMap=@{}
    foreach($omEntry in $Tree){
        $omSize=if($omEntry -is [IO.FileInfo]){$omEntry.Length}else{0L}
        $omMap[$omEntry.FullName]="$($omEntry.GetType().Name)|$omSize|$($omEntry.CreationTimeUtc.Ticks)|$($omEntry.LastWriteTimeUtc.Ticks)|$([int]$omEntry.Attributes)"
    }
    return $omMap
}
function Assert-OmSameTree($Before,$After) {
    if($Before.Count -ne $After.Count){throw 'Candidate entry count changed during cleanup'}
    foreach($omKey in $Before.Keys){if(-not $After.ContainsKey($omKey) -or $After[$omKey] -ne $Before[$omKey]){throw "Candidate metadata changed: $omKey"}}
}
function Get-OmProcesses {
    return @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='node.exe' OR Name='pwsh.exe' OR Name='powershell.exe'" |
        Where-Object ProcessId -ne $PID)
}
function Get-OmApplyBlockers {
    # Refuse, never terminate processes. The resident REAL service may stay up:
    # it uses -B and its database/media/config are not eligible paths.
    return @(Get-OmProcesses | Where-Object {
        $omCommand=[string]$_.CommandLine
        (($_.Name -in @('python.exe','pythonw.exe','node.exe')) -and
            ($omCommand -match 'core-vision-(release|final)(?![a-zA-Z0-9_])' -or
             $omCommand -match '(verify-live-vision|runtime-stability|probe-item-recognition)\.py')) -or
        (($_.Name -in @('pwsh.exe','powershell.exe')) -and
            $omCommand -match '(?i)(?:^|\s)-(?:File|f)\s+(?:"[^"]*measure-vision-stability\.ps1"|''[^'']*measure-vision-stability\.ps1''|[^\s]*measure-vision-stability\.ps1)(?:\s|$)')
    } | ForEach-Object ProcessId)
}
function Assert-OmInactive([string]$Target,[string]$Kind) {
    $omLeaf=[regex]::Escape([IO.Path]::GetFileName($Target))
    $omActive=@(Get-OmProcesses | Where-Object {
        $omCommand=[string]$_.CommandLine
        $omCommand.Contains($Target,[StringComparison]::OrdinalIgnoreCase) -or
        $omCommand.Contains($Target.Replace('\','/'),[StringComparison]::OrdinalIgnoreCase) -or
        ($Kind -eq 'completed_test_basetemp' -and $omCommand -match ('(?<![a-zA-Z0-9_-])'+$omLeaf+'(?![a-zA-Z0-9_-])'))
    })
    if($omActive.Count){throw ('Active process references this candidate: '+(($omActive.ProcessId)-join ','))}
}
function Copy-OmEvidence([string]$Source,[string]$Destination) {
    $null=Assert-OmNormalPath $Source
    $null=Assert-OmNormalPath $Destination $omEvidence -AllowMissing
    $omParent=Split-Path $Destination -Parent
    New-Item -ItemType Directory -Path $omParent -Force | Out-Null
    $null=Assert-OmNormalPath $Destination $omEvidence -AllowMissing
    $omHash=(Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash
    if(Test-Path -LiteralPath $Destination){
        if((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $omHash){throw 'Preserved evidence conflict'}
    }else{
        Copy-Item -LiteralPath $Source -Destination $Destination
    }
    $null=Assert-OmNormalPath $Destination $omEvidence
    if((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $omHash -or
       (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash -ne $omHash){throw 'Evidence source/copy changed'}
    return $omHash
}
$null=Assert-OmNormalPath $omEvidence (Join-Path $omRoot 'data\verification')
$omInventoryPath=Join-Path $omEvidence 'cleanup-candidates.json'
$null=Assert-OmNormalPath $omInventoryPath $omEvidence
$omInventoryHash=(Get-FileHash -LiteralPath $omInventoryPath -Algorithm SHA256).Hash
$omInventory=Get-Content -LiteralPath $omInventoryPath -Raw | ConvertFrom-Json
if($omInventory.project -ne $omRoot){throw 'Inventory does not belong to this project'}
$omApplyBlockers=@(Get-OmApplyBlockers)
if($Apply -and $omApplyBlockers.Count){throw ('Apply must wait for release/final tests or sampling to finish: '+($omApplyBlockers -join ','))}
$omBefore=Get-OmBytes $omRoot
$omReferenceRoot=Join-Path $omRoot 'data\registered-items'
function Get-OmReferenceHashes {
    $omHashes=@{}
    $null=Assert-OmNormalPath $omReferenceRoot -AllowMissing
    if(Test-Path -LiteralPath $omReferenceRoot){
        foreach($omReference in (Get-OmSafeTree $omReferenceRoot)){
            if($omReference -is [IO.FileInfo]){
                $omHashes[$omReference.FullName]=(Get-FileHash -LiteralPath $omReference.FullName -Algorithm SHA256).Hash
            }
        }
    }
    return $omHashes
}
$omRefBefore=Get-OmReferenceHashes
foreach($omCandidate in $omInventory.candidate_roots){
    $omPath=[IO.Path]::GetFullPath($omCandidate.path)
    $omRow=[ordered]@{path=$omPath;kind=$omCandidate.kind;status='skipped';bytes=0L;files=0;screenshots=0;videos=0;reason=$null}
    $omDeleteStarted=$false
    try {
        if(-not $omPath.StartsWith($omRoot+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Outside project'}
        if($omPath -match '\\(?:node_modules|\.venv[^\\]*|registered-items|database|backups|real-media|demo-media|firmware)(\\|$)'){throw 'Protected path'}
        $omAllowed=($omCandidate.kind -eq 'completed_test_basetemp' -and $omCandidate.owner_exit_confirmed -eq $true -and
            ($omPath.StartsWith((Join-Path $omRoot 'data\temporary')+'\') -or
             $omPath -eq (Join-Path $omRoot 'data\verification\core-repair-20260907\optin-test-temp')) -and
            $omPath -notmatch 'core-vision-release') -or
            ($omCandidate.kind -eq 'rebuildable_code_cache' -and (Split-Path $omPath -Leaf) -in @('__pycache__','.pytest_cache')) -or
            ($omCandidate.kind -eq 'isolated_optional_yoloe_runtime' -and $omPath -eq (Join-Path $omRoot 'data\model-evaluation\python-packages')) -or
            ($omCandidate.kind -eq 'failed_optional_downloads' -and $omPath -eq (Join-Path $omRoot 'data\model-evaluation\wheels'))
        if(-not $omAllowed){throw 'Not an explicitly approved candidate type'}
        if(-not(Test-Path -LiteralPath $omPath)){throw 'Already absent'}
        $null=Assert-OmNormalPath $omPath
        $omTree=Get-OmSafeTree $omPath
        $omMetadata=Get-OmMetadata $omTree
        $omFiles=@($omTree|Where-Object {$_ -is [IO.FileInfo]})
        $omRow.bytes=[long]($omFiles|Measure-Object Length -Sum).Sum
        $omRow.files=$omFiles.Count
        $omRow.screenshots=@($omFiles|Where-Object Extension -in @('.jpg','.jpeg','.png','.webp')).Count
        $omRow.videos=@($omFiles|Where-Object Extension -in @('.mp4','.avi','.webm')).Count
        if($omRow.bytes -ne $omCandidate.bytes -or $omRow.files -ne $omCandidate.files){throw 'Candidate contents changed since reviewed inventory'}
        Assert-OmInactive $omPath $omCandidate.kind
        $omRow.status='would_delete'
        if($Apply){
            if(@(Get-OmApplyBlockers).Count){throw 'A release/final test or sampler started; refusing Apply'}
            if((Get-FileHash -LiteralPath $omInventoryPath -Algorithm SHA256).Hash -ne $omInventoryHash){throw 'Reviewed inventory changed'}
            foreach($omDiagnostic in $omCandidate.diagnostic_files){
                $omSource=[IO.Path]::GetFullPath($omDiagnostic.path)
                if(-not $omSource.StartsWith($omPath+'\',[StringComparison]::OrdinalIgnoreCase) -or -not $omMetadata.ContainsKey($omSource)){throw 'Diagnostic outside scanned candidate'}
                $omRelative=$omSource.Substring($omPath.Length+1)
                $omDestination=Join-Path (Join-Path (Join-Path $omEvidence 'preserved-test-evidence') (Split-Path $omPath -Leaf)) $omRelative
                $omHash=Copy-OmEvidence $omSource $omDestination
                $omPreserved.Add(@{source=$omSource;destination=$omDestination;sha256=$omHash})
            }
            $null=Assert-OmNormalPath $omPath
            Assert-OmSameTree $omMetadata (Get-OmMetadata (Get-OmSafeTree $omPath))
            Assert-OmInactive $omPath $omCandidate.kind
            if(@(Get-OmApplyBlockers).Count){throw 'A release/final test or sampler started before deletion'}
            # Every ancestor and child was checked, and the final absolute target
            # is a named reviewed subdirectory, never a workspace/data root.
            $omDeleteStarted=$true
            Remove-Item -LiteralPath $omPath -Recurse -Force
            if(Test-Path -LiteralPath $omPath){throw 'Directory still exists'}
            $omRow.status='deleted'
        }
    } catch {
        $omRow.reason=$_.Exception.Message
        $omRow.status=if($omDeleteStarted){'deletion_incomplete'}else{'skipped'}
    }
    $omRows.Add([pscustomobject]$omRow)
}
$omRefAfter=Get-OmReferenceHashes
if($omRefBefore.Count -ne $omRefAfter.Count){throw 'Protected reference collection changed'}
foreach($omReferencePath in $omRefBefore.Keys){
    if($omRefAfter[$omReferencePath] -ne $omRefBefore[$omReferencePath]){throw 'Protected reference image changed'}
}
$omAfter=Get-OmBytes $omRoot
$omDeleted=@($omRows|Where-Object status -eq 'deleted')
$omResult=[ordered]@{timestamp_utc=(Get-Date).ToUniversalTime().ToString('o');dry_run=(-not $Apply);project=$omRoot;apply_blocker_pids=$omApplyBlockers;
    project_logical_bytes_before=$omBefore;project_logical_bytes_after=$omAfter;project_net_change_bytes=$omAfter-$omBefore;
    deleted_logical_bytes=[long]($omDeleted|Measure-Object bytes -Sum).Sum;deleted_files=[int]($omDeleted|Measure-Object files -Sum).Sum;
    deleted_test_screenshots=[int]($omDeleted|Measure-Object screenshots -Sum).Sum;deleted_test_videos=[int]($omDeleted|Measure-Object videos -Sum).Sum;
    deleted_real_events=0;deleted_real_media=0;deleted_reference_images=0;protected_reference_sha256=$omRefAfter;
    irreversible_rebuildable_files_only=$true;rows=$omRows;preserved_evidence=$omPreserved;
    accounting_note='Logical sizes exclude reparse targets. Other running tests may change total project size concurrently. Any skipped link tree is preserved, not followed. Deletion totals count only fully removed targets; a deletion_incomplete row is not claimed as fully deleted or fully preserved.'}
$omName=if($Apply){'cleanup-result.json'}else{'cleanup-dry-run.json'}
$omReportPath=Join-Path $omEvidence $omName
$null=Assert-OmNormalPath $omReportPath $omEvidence -AllowMissing
[IO.File]::WriteAllText($omReportPath,($omResult|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
[pscustomobject]$omResult|Select-Object dry_run,deleted_logical_bytes,deleted_files,deleted_test_screenshots,deleted_test_videos,project_logical_bytes_before,project_logical_bytes_after|ConvertTo-Json -Compress
