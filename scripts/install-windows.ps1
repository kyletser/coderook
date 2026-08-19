param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/ first."
}

$arguments = @("tool", "install", "--from", (Resolve-Path "$PSScriptRoot\.."), "CodeRook")
if ($Force) {
    $arguments = @("tool", "install", "--force", "--from", (Resolve-Path "$PSScriptRoot\.."), "CodeRook")
}
& $uv.Source @arguments
if ($LASTEXITCODE -ne 0) {
    throw "CodeRook installation failed with exit code $LASTEXITCODE"
}

Write-Host "CodeRook installed. Run: coderook doctor all"
