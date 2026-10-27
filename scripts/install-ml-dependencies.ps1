$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Run scripts\setup-windows.bat before installing optional ML dependencies."
}
$version = (& $pythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not determine Python version." }
switch ($version) {
    "3.11" { $pythonKey = "py311" }
    "3.12" { $pythonKey = "py312" }
    default { throw "Unsupported Python $version. Install Python 3.11 or 3.12." }
}
$lock = Join-Path $projectRoot "backend\requirements-ml-lock-win-$pythonKey.txt"
if (-not (Test-Path -LiteralPath $lock -PathType Leaf)) {
    throw "Optional ML lock for Windows/Python $version not found: $lock"
}
$validator = Join-Path $projectRoot "scripts\validate_dependency_locks.py"
& $pythonPath $validator --os-key win --python-key $pythonKey
if ($LASTEXITCODE -ne 0) {
    throw "Dependency manifests and locks disagree. Refresh and commit the dependency lock matrix before installing optional ML packages."
}
Write-Output "Installing optional ML dependencies from $lock"
& $pythonPath -m pip install --require-hashes -r $lock
if ($LASTEXITCODE -ne 0) { throw "Hash-verified optional ML dependency installation failed." }
Write-Output "Optional transcription dependencies installed. Model weights remain separate."
