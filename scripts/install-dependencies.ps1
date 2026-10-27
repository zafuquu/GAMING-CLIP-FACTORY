$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$requirementsRoot = Join-Path $projectRoot "backend"
$frontendRoot = Join-Path $projectRoot "frontend"

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "Python virtual environment is missing: $pythonPath"
}
$version = (& $pythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0) { throw "Could not determine the virtual environment's Python version." }
switch ($version) {
    "3.11" { $pythonKey = "py311" }
    "3.12" { $pythonKey = "py312" }
    default { throw "Unsupported Python $version. Install Python 3.11 or 3.12." }
}
$lockPath = Join-Path $requirementsRoot "requirements-lock-win-$pythonKey.txt"
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
    throw "The verified dependency lock for Windows/Python $version is missing: $lockPath"
}

# Never bless a stale/mismatched dependency matrix as installed.
$validator = Join-Path $projectRoot "scripts\validate_dependency_locks.py"
& $pythonPath $validator --os-key win --python-key $pythonKey
if ($LASTEXITCODE -ne 0) {
    throw "Dependency manifests and lock files disagree. Refresh and commit the complete lock matrix before starting."
}

Write-Output "Installing exact hashed dependencies from $lockPath"
& $pythonPath -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip." }
& $pythonPath -m pip install --require-hashes -r $lockPath
if ($LASTEXITCODE -ne 0) { throw "Hash-verified backend dependency installation failed." }

Push-Location $frontendRoot
try {
    & npm ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed." }
} finally {
    Pop-Location
}

& (Join-Path $PSScriptRoot "lock-state.ps1") -Mode write
if ($LASTEXITCODE -ne 0) { throw "Could not record installed dependency fingerprints." }
Write-Output "Verified dependency setup completed successfully."
