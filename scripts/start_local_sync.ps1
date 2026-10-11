param(
    [Parameter(Mandatory=$true)][string]$Configuration,
    [Parameter(Mandatory=$true)][string]$StateDirectory
)
$ErrorActionPreference='Stop'
$taskWorkspace=Split-Path -Parent $PSScriptRoot
$taskPython=Join-Path $taskWorkspace '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Local pipeline runtime is missing.' }
Set-Location -LiteralPath $taskWorkspace
# The daemon holds an OS file lock throughout every poll, DB retry and import.
# Scheduled Task IgnoreNew is supplementary; repeated manual starts are safe.
& $taskPython (Join-Path $PSScriptRoot 'local_sync_daemon.py') --configuration $Configuration --root $StateDirectory
exit $LASTEXITCODE
