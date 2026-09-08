param(
    [ValidateRange(5,3600)][int]$Seconds=1800,
    [ValidateRange(2,60)][int]$IntervalSeconds=10,
    [ValidateNotNullOrEmpty()][string]$Output='data/verification/core-vision/runtime-stability-current.json'
)
$ErrorActionPreference='Stop'
$omProject=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$omData=Join-Path $omProject 'data'
$omVerification=[IO.Path]::GetFullPath((Join-Path $omData 'verification'))
$omReport=[IO.Path]::GetFullPath($(if([IO.Path]::IsPathRooted($Output)){$Output}else{Join-Path $omProject $Output}))
if(-not $omReport.StartsWith($omVerification+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetExtension($omReport) -ne '.json'){
    throw 'Output must be a JSON file inside project data/verification.'
}
# Refuse links in the destination chain before any process or API access.
$omOutputParent=$omReport
while($omOutputParent -and $omOutputParent.StartsWith($omProject,[StringComparison]::OrdinalIgnoreCase)){
    if(Test-Path -LiteralPath $omOutputParent){
        $omOutputItem=Get-Item -LiteralPath $omOutputParent -Force
        if($omOutputItem.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Output must not traverse a link or junction.'}
    }
    $omOutputParent=[IO.Path]::GetDirectoryName($omOutputParent)
}
if($PSBoundParameters.ContainsKey('Output') -and (Test-Path -LiteralPath $omReport)){
    throw 'Explicit Output must be a new file; existing verification evidence is preserved.'
}
function Write-OmReportAtomic($omValue){
    $omReportDirectory=[IO.Path]::GetDirectoryName($omReport)
    [void][IO.Directory]::CreateDirectory($omReportDirectory)
    $omTemporary=Join-Path $omReportDirectory ('.'+[IO.Path]::GetFileName($omReport)+'.'+[guid]::NewGuid().ToString('N')+'.tmp')
    try{
        [IO.File]::WriteAllText($omTemporary,($omValue|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
        if([IO.File]::Exists($omReport)){
            if([IO.File].GetMethod('Move',[type[]]@([string],[string],[bool]))){
                [IO.File]::Move($omTemporary,$omReport,$true)
            }else{
                [IO.File]::Replace($omTemporary,$omReport,[NullString]::Value)
            }
        }else{
            [IO.File]::Move($omTemporary,$omReport)
        }
    }finally{
        if([IO.File]::Exists($omTemporary)){[IO.File]::Delete($omTemporary)}
    }
}
$omPidText=Get-Content -LiteralPath (Join-Path $omData 'diagnostics\photo-memory.pid') -Raw
$omProcessId=[int]$omPidText.Trim()
$omCommand=Get-CimInstance Win32_Process -Filter "ProcessId=$omProcessId"
if (-not $omCommand -or $omCommand.CommandLine -notmatch 'scripts[/\\]serve\.py' -or $omCommand.CommandLine -notmatch '--mode REAL' -or $omCommand.CommandLine -notmatch '--port 8018') { throw 'PID does not describe the owned REAL ObjectMemory service.' }
if (-not (Get-NetTCPConnection -LocalPort 8018 -State Listen | Where-Object OwningProcess -eq $omProcessId)) { throw 'Owned process is not listening on 8018.' }
function Get-OmMediaBytes {
    $omTotal=0L
    foreach($omFolder in @('real-media','database')) {
        $omPath=Join-Path $omData $omFolder
        if(Test-Path -LiteralPath $omPath){ $omSize=(Get-ChildItem -LiteralPath $omPath -File -Recurse -Force -ErrorAction Stop | Measure-Object Length -Sum).Sum; if($omSize){$omTotal += [long]$omSize} }
    }
    return $omTotal
}
$omStart=Get-Date
$omEnd=$omStart.AddSeconds($Seconds)
$omRows=[System.Collections.Generic.List[object]]::new()
$omFailures=[System.Collections.Generic.List[string]]::new()
$omBefore=Get-OmMediaBytes
$omPreviousCpu=$null
$omPreviousTime=$null
$omCores=[Environment]::ProcessorCount
while((Get-Date) -lt $omEnd) {
    try {
        $omProcess=Get-Process -Id $omProcessId -ErrorAction Stop
        $omTime=Get-Date
        $omCpu=[double]$omProcess.CPU
        $omElapsed=($omTime-$omStart).TotalSeconds
        $omPercent=if($null -ne $omPreviousCpu){100*($omCpu-$omPreviousCpu)/(($omTime-$omPreviousTime).TotalSeconds*$omCores)}else{$null}
        $omHealth=Invoke-RestMethod -Uri 'http://127.0.0.1:8018/api/health' -TimeoutSec 5
        if($omHealth.runtime_mode -ne 'REAL'){throw 'Mode changed during measurement'}
        $omRows.Add([ordered]@{elapsed_seconds=[math]::Round($omElapsed,3);resident_bytes=$omProcess.WorkingSet64;private_bytes=$omProcess.PrivateMemorySize64;threads=$omProcess.Threads.Count;handles=$omProcess.HandleCount;cpu_percent_all_cores=$omPercent;healthy=$true})
        $omPreviousCpu=$omCpu;$omPreviousTime=$omTime
    } catch { $omFailures.Add($_.Exception.GetType().Name);break }
    $omPartial=[ordered]@{status='running';started_at=$omStart.ToUniversalTime().ToString('o');requested_seconds=$Seconds;process_id=$omProcessId;api_mock=$false;read_only=$true;real_media_database_bytes_before=$omBefore;samples=$omRows;errors=$omFailures}
    Write-OmReportAtomic $omPartial
    Start-Sleep -Seconds ([math]::Min($IntervalSeconds,[math]::Max(1,($omEnd-(Get-Date)).TotalSeconds)))
}
$omFinished=Get-Date
$omGaps=for($omIndex=1;$omIndex -lt $omRows.Count;$omIndex++){ $omRows[$omIndex].elapsed_seconds-$omRows[$omIndex-1].elapsed_seconds }
$omMaxGap=if($omGaps){[double]($omGaps|Measure-Object -Maximum).Maximum}else{$Seconds}
$omTail=if($omRows.Count){($omFinished-$omStart).TotalSeconds-$omRows[$omRows.Count-1].elapsed_seconds}else{$Seconds}
if($omMaxGap -gt [math]::Max(30,$IntervalSeconds*3) -or $omTail -gt [math]::Max(20,$IntervalSeconds*2)){$omFailures.Add('UnobservedSamplingGap')}
$omResult=[ordered]@{status='finished';started_at=$omStart.ToUniversalTime().ToString('o');ended_at=$omFinished.ToUniversalTime().ToString('o');requested_seconds=$Seconds;elapsed_seconds=($omFinished-$omStart).TotalSeconds;process_id=$omProcessId;api_mock=$false;read_only=$true;samples=$omRows;errors=$omFailures;real_media_database_bytes_before=$omBefore;real_media_database_bytes_after=(Get-OmMediaBytes);complete_window=($omFailures.Count -eq 0 -and ($omFinished-$omStart).TotalSeconds -ge $Seconds);physical_item_accuracy_proven=$false}
$omResult['max_sample_gap_seconds']=$omMaxGap
$omResult['unobserved_tail_seconds']=$omTail
Write-OmReportAtomic $omResult
Write-Output ([pscustomobject]$omResult|Select-Object status,elapsed_seconds,complete_window,real_media_database_bytes_before,real_media_database_bytes_after|ConvertTo-Json -Compress)
if(-not $omResult.complete_window){exit 1}
