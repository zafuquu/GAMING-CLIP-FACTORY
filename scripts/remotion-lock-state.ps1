param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("test", "write")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"
$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$remotionRoot = Join-Path $projectRoot "remotion"
$marker = Join-Path $remotionRoot ".gcf-dependency-locks.sha256"
$packageManifest = Join-Path $remotionRoot "package.json"
$packageLock = Join-Path $remotionRoot "package-lock.json"

foreach ($required in @($packageManifest, $packageLock)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        Write-Error "Required Remotion dependency file is missing: $required"
        exit 2
    }
}

function Get-GcfSha256 {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNullOrEmpty()]
        [string]$LiteralPath
    )
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($LiteralPath)
    try {
        return ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

$expected = @(
    "package-json=$(Get-GcfSha256 -LiteralPath $packageManifest)"
    "package-lock=$(Get-GcfSha256 -LiteralPath $packageLock)"
) -join [Environment]::NewLine

if ($Mode -eq "test") {
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { exit 1 }
    $actual = [System.IO.File]::ReadAllText($marker).TrimEnd()
    if ($actual -cne $expected) { exit 1 }
    exit 0
}

[System.IO.File]::WriteAllText($marker, $expected, [System.Text.UTF8Encoding]::new($false))
Write-Output "Remotion dependency lock fingerprint recorded."
