param(
    [string]$Version = $env:CODEROOK_VERSION,
    [string]$Repository = "kyletser/coderook",
    [string]$InstallRoot = "$env:LOCALAPPDATA\CodeRook",
    [string]$BinDir = "$env:USERPROFILE\.local\bin"
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($Version)) {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/releases/latest"
    $Version = [string]$release.tag_name
}
if ([string]::IsNullOrWhiteSpace($Version)) {
    throw "Could not resolve a CodeRook release version."
}

$asset = "coderook-windows-x86_64.zip"
$baseUrl = "https://github.com/$Repository/releases/download/$Version"
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("coderook-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $archive = Join-Path $temporary $asset
    $checksums = Join-Path $temporary "SHA256SUMS"
    Invoke-WebRequest -Uri "$baseUrl/$asset" -OutFile $archive
    Invoke-WebRequest -Uri "$baseUrl/SHA256SUMS" -OutFile $checksums
    $line = Get-Content -LiteralPath $checksums |
        Where-Object { $_ -match "^[0-9a-fA-F]{64}\s+\*?$([regex]::Escape($asset))$" } |
        Select-Object -First 1
    if ($null -eq $line) {
        throw "Checksum entry for $asset is missing."
    }
    $expected = ($line -split "\s+")[0].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) {
        throw "Checksum verification failed for $asset."
    }

    $versionDir = Join-Path $InstallRoot $Version
    New-Item -ItemType Directory -Force -Path $versionDir, $BinDir | Out-Null
    Expand-Archive -LiteralPath $archive -DestinationPath $versionDir -Force
    $launcher = Join-Path $versionDir "coderook-windows-x86_64\coderook.cmd"
    $shim = Join-Path $BinDir "coderook.cmd"
    Set-Content -LiteralPath $shim -Encoding ASCII -Value "@echo off`r`ncall `"$launcher`" %*"
    Write-Host "CodeRook $Version installed at $versionDir"
    Write-Host "Run: $shim"
} finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
