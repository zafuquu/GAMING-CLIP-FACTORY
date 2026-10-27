$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$backendRoot = Join-Path $projectRoot "backend"
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$logDirectory = Join-Path $projectRoot "data\logs"
$stdoutPath = Join-Path $logDirectory "backend.stdout.log"
$stderrPath = Join-Path $logDirectory "backend.stderr.log"
$pidPath = Join-Path $logDirectory "backend.pid"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Write-Error "The application virtual environment is missing: $pythonPath"
    exit 2
}
if (-not (Test-Path -LiteralPath $backendRoot -PathType Container)) {
    Write-Error "The backend directory is missing: $backendRoot"
    exit 2
}

$pythonVersion = (& $pythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0 -or $pythonVersion -notin @("3.11", "3.12")) {
    Write-Error "Unsupported virtual-environment Python version '$pythonVersion'. Re-run scripts\start.bat to rebuild it with Python 3.11 or 3.12."
    exit 2
}

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$env:GCF_PROJECT_ROOT = $projectRoot

# Avoid spawning a second worker if the previous process exists but the API has not
# become responsive yet. The caller will continue its bounded readiness polling.
$existing = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and
    ([System.IO.Path]::GetFullPath($_.ExecutablePath) -ieq [System.IO.Path]::GetFullPath($pythonPath)) -and
    $_.CommandLine -match '(?:^|\s)-m\s+app(?:\s|$)'
}
if ($existing) {
    $ids = ($existing | ForEach-Object { $_.ProcessId }) -join ", "
    Write-Output "An existing Gaming Clip Factory backend process is still running (PID $ids); waiting for readiness instead of duplicating the worker."
    exit 0
}

$process = Start-Process -FilePath $pythonPath -ArgumentList @("-m", "app") -WorkingDirectory $backendRoot -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru
[System.IO.File]::WriteAllText($pidPath, [string]$process.Id, [System.Text.Encoding]::ASCII)
Write-Output "Started backend PID $($process.Id)"
Write-Output "Backend logs: $stdoutPath and $stderrPath"

Start-Sleep -Seconds 2
$process.Refresh()
if ($process.HasExited) {
    Write-Error "The backend process exited during startup with code $($process.ExitCode)."
    if (Test-Path -LiteralPath $stderrPath) {
        Write-Output "----- backend.stderr.log -----"
        Get-Content -LiteralPath $stderrPath -Tail 100
    }
    if (Test-Path -LiteralPath $stdoutPath) {
        Write-Output "----- backend.stdout.log -----"
        Get-Content -LiteralPath $stdoutPath -Tail 100
    }
    exit 1
}
