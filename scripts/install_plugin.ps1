param(
    [Parameter(Mandatory=$true)]
    [string]$ComfyUICustomNodes
)

$ErrorActionPreference = "Stop"
$source = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\comfyui_plugin\vram_guardian_comfyui")
$targetRoot = Resolve-Path -LiteralPath $ComfyUICustomNodes
$target = Join-Path $targetRoot "vram_guardian_comfyui"

if (Test-Path -LiteralPath $target) {
    Remove-Item -LiteralPath $target -Recurse -Force
}

Copy-Item -LiteralPath $source -Destination $target -Recurse
Write-Host "Installed VRAM Guardian ComfyUI plugin to $target"
