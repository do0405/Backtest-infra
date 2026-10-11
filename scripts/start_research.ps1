param([string]$PythonPath)
$ErrorActionPreference='Stop'
$taskWorkspace=Split-Path -Parent $PSScriptRoot
if (-not $PythonPath) {
    $taskLocalRuntime=Join-Path $env:USERPROFILE 'AppData\Local\BacktestResearch\venv\Scripts\python.exe'
    $PythonPath=$taskLocalRuntime
}
$taskPython=(Resolve-Path -LiteralPath $PythonPath).Path
$taskExisting=Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($taskExisting) {
    $taskProcess=Get-CimInstance Win32_Process -Filter ('ProcessId='+$taskExisting[0].OwningProcess)
    $taskCompatible=$taskProcess.ExecutablePath -eq $taskPython -and $taskProcess.CommandLine -match '\bbacktest_research\.api\b'
    if (-not $taskCompatible -and $taskProcess.CommandLine -match '\bbacktest_research\.api\b') {
        $taskLauncher=Get-CimInstance Win32_Process -Filter ('ProcessId='+$taskProcess.ParentProcessId)
        $taskVenvRoot=Split-Path -Parent (Split-Path -Parent $taskPython)
        $taskVenvConfig=Join-Path $taskVenvRoot 'pyvenv.cfg'
        $taskBaseExecutable=$null
        if (Test-Path -LiteralPath $taskVenvConfig) {
            $taskHomeLine=Get-Content -LiteralPath $taskVenvConfig | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1
            if ($taskHomeLine) { $taskBaseExecutable=Join-Path (($taskHomeLine -split '=',2)[1].Trim()) 'python.exe' }
        }
        $taskCompatible=$taskLauncher.ExecutablePath -eq $taskPython -and $taskLauncher.CommandLine -match '\bbacktest_research\.api\b' -and $taskProcess.ExecutablePath -eq $taskBaseExecutable
    }
    if (-not $taskCompatible) {
        throw 'Research port is occupied by another process; no process was stopped.'
    }
    Write-Output 'Existing research server: http://127.0.0.1:8765'
    exit 0
}
$taskOutput=Join-Path $taskWorkspace 'artifacts\research-platform'
New-Item -ItemType Directory -Path $taskOutput -Force | Out-Null
$taskPriorPythonPath=$env:PYTHONPATH
try {
    $env:PYTHONPATH=Join-Path $taskWorkspace 'src'
    $taskProcess=Start-Process -FilePath $taskPython -ArgumentList '-m','backtest_research.api' -WorkingDirectory $taskWorkspace -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskOutput 'server.stdout') -RedirectStandardError (Join-Path $taskOutput 'server.stderr')
    @{ pid=$taskProcess.Id; launched_at=[DateTime]::UtcNow.ToString('o'); executable=$taskPython; url='http://127.0.0.1:8765'; role='local_research_ui'; ingestion_owner=$false } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskOutput 'server-identity.json') -Encoding utf8
} finally { $env:PYTHONPATH=$taskPriorPythonPath }
Write-Output 'Research server starting: http://127.0.0.1:8765'
