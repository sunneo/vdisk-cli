# Build vdi.exe (single file). Run from a Developer PowerShell:
#   packaging\build.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

python -m pip install -q -e ".[dev]"
pyinstaller --clean -y packaging\vdi.spec

$ver = python -c "import sys;sys.path.insert(0,'src');import vdi;print(vdi.__version__)"
$dst = "dist\vdi-$ver-windows"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item dist\vdi.exe $dst\
Copy-Item README.md,DESIGN.md $dst\
Compress-Archive -Force -Path $dst\* -DestinationPath "dist\vdi-$ver-windows.zip"
Write-Output "built dist\vdi-$ver-windows.zip"
Write-Output "note: put qemu-system-x86_64.exe next to vdi.exe in appliance\qemu\ for --engine qemu"
