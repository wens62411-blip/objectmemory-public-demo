param([switch]$Apply,[string]$Database)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$python=Join-Path $root '.venv\Scripts\python.exe'
$resultName=if ($Apply) {'audit\cleanup-result.json'} else {'audit\cleanup-dry-run.json'}
$result=Join-Path $root $resultName
$arguments=@((Join-Path $PSScriptRoot 'event_audit.py'),'--cleanup','--result',$result,'--json',(Join-Path $root 'audit\event-audit.json'),'--csv',(Join-Path $root 'audit\event-audit.csv'))
if ($Database) {$arguments+=@('--database',$Database)}
if ($Apply) {$arguments+='--apply'}
& $python @arguments
if ($LASTEXITCODE -ne 0) {throw 'Event cleanup failed.'}
if (-not $Apply) {Write-Host 'Dry-run only. Review audit/cleanup-dry-run.json; use -Apply only after classification is accepted.' -ForegroundColor Yellow}
