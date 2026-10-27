param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("test", "write")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$marker = Join-Path $projectRoot ".venv\.gcf-dependency-locks.sha256"
$pythonLock = Join-Path $projectRoot "backend\requirements-lock.txt"
$npmLock = Join-Path $projectRoot "frontend\package-lock.json"

foreach ($required in @($pythonLock, $npmLock)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        Write-Error "Required dependency lockfile is missing: $required"
        exit 2
    }
}

$pythonHash = (Get-FileHash -LiteralPath $pythonLock -Algorithm SHA256).Hash.ToLowerInvariant()
$npmHash = (Get-FileHash -LiteralPath $npmLock -Algorithm SHA256).Hash.ToLowerInvariant()
$expected = "python=$pythonHash" + [Environment]::NewLine + "npm=$npmHash"

if ($Mode -eq "test") {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { exit 1 }
    $actual = [System.IO.File]::ReadAllText($marker).TrimEnd()
    if ($actual -cne $expected) { exit 1 }
    exit 0
}

$venv = Join-Path $projectRoot ".venv"
if (-not (Test-Path -LiteralPath $venv -PathType Container)) {
    Write-Error "Cannot record dependency hashes before the virtual environment has been created."
    exit 2
}
[System.IO.File]::WriteAllText($marker, $expected, [System.Text.UTF8Encoding]::new($false))
Write-Output "Dependency lock fingerprints recorded."
