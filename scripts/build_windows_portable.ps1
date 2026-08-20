param(
    [string]$Output = "dist\coderook-windows-portable"
)

$ErrorActionPreference = "Stop"
$repository = Resolve-Path "$PSScriptRoot\.."
$target = [System.IO.Path]::GetFullPath((Join-Path $repository $Output))
$distRoot = [System.IO.Path]::GetFullPath((Join-Path $repository "dist"))
$relativeTarget = [System.IO.Path]::GetRelativePath($distRoot, $target)
if (
    [System.IO.Path]::IsPathRooted($relativeTarget) -or
    $relativeTarget -eq "." -or
    $relativeTarget -eq ".." -or
    $relativeTarget.StartsWith("..$([System.IO.Path]::DirectorySeparatorChar)")
) {
    throw "Portable output must stay under $distRoot"
}

& uv build --wheel $repository
if ($LASTEXITCODE -ne 0) {
    throw "wheel build failed"
}
$wheel = Get-ChildItem -LiteralPath $distRoot -Filter "coderook-*.whl" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($null -eq $wheel) {
    throw "built wheel was not found"
}
$pythonExe = (& uv --directory ([System.IO.Path]::GetTempPath()) python find --managed-python 3.12).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonExe)) {
    throw "uv-managed Python 3.12 was not found"
}
$pythonRoot = Split-Path -Parent $pythonExe
if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}
$runtime = Join-Path $target "runtime"
New-Item -ItemType Directory -Force -Path $target | Out-Null
Copy-Item -LiteralPath $pythonRoot -Destination $runtime -Recurse
$portablePython = Join-Path $runtime "python.exe"
& uv pip install --python $portablePython --break-system-packages $wheel.FullName
if ($LASTEXITCODE -ne 0) {
    throw "portable dependency installation failed"
}
Copy-Item -LiteralPath "$PSScriptRoot\portable-coderook.cmd" -Destination $target -Force
Compress-Archive -Path "$target\*" -DestinationPath "$target.zip" -Force
Write-Host "Portable archive: $target.zip"
