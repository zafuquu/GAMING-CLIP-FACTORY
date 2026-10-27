$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$pythonPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot ".venv\Scripts\python.exe"))
$pidFile = Join-Path $projectRoot "data\logs\backend.pid"
$taskkillPath = Join-Path $env:WINDIR "System32\taskkill.exe"
$stoppedIds = [System.Collections.Generic.HashSet[int]]::new()

function Stop-BackendProcessTree([int]$ProcessId) {
    $processInfo = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $ProcessId) -ErrorAction SilentlyContinue
    if (-not $processInfo) { return }
    if (-not $processInfo.ExecutablePath) { return }
    $actualPython = [System.IO.Path]::GetFullPath($processInfo.ExecutablePath)
    if ($actualPython -ine $pythonPath) { return }
    if ($processInfo.CommandLine -notmatch '(?:^|\s)-m\s+app(?:\s|$)') { return }

    # Kill the process tree, not just uvicorn/Python. This also terminates a live FFmpeg
    # child so stopping the app cannot leave a render burning CPU in the background.
    & $taskkillPath /PID $ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -eq 0) {
        [void]$stoppedIds.Add($ProcessId)
        Write-Output "Stopped Gaming Clip Factory process tree (PID $ProcessId)."
    }
}

if (Test-Path -LiteralPath $pidFile) {
    $raw = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    $parsedId = 0
    if ([int]::TryParse($raw, [ref]$parsedId) -and $parsedId -gt 0) {
        Stop-BackendProcessTree -ProcessId $parsedId
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

# Recover if the PID file was lost or stale, but only target the exact virtualenv
# executable belonging to this checkout and the expected application command.
$matchingProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ExecutablePath -and
    ([System.IO.Path]::GetFullPath($_.ExecutablePath) -ieq $pythonPath) -and
    $_.CommandLine -match '(?:^|\s)-m\s+app(?:\s|$)'
}
foreach ($processInfo in $matchingProcesses) {
    Stop-BackendProcessTree -ProcessId ([int]$processInfo.ProcessId)
}

if ($stoppedIds.Count -gt 0) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Write-Output "Gaming Clip Factory AI backend stopped."
} else {
    Write-Output "No running Gaming Clip Factory AI backend was found for this project."
}
