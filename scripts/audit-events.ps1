param([string]$Database)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$python=Join-Path $root '.venv\Scripts\python.exe'
$arguments=@((Join-Path $PSScriptRoot 'event_audit.py'),'--json',(Join-Path $root 'audit\event-audit.json'),'--csv',(Join-Path $root 'audit\event-audit.csv'))
if ($Database) {$arguments+=@('--database',$Database)}
& $python @arguments
if ($LASTEXITCODE -ne 0) {throw 'Event audit failed.'}
