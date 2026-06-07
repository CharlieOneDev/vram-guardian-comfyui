#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 /path/to/ComfyUI/custom_nodes" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${SCRIPT_DIR}/../comfyui_plugin/vram_guardian_comfyui"
TARGET="$1/vram_guardian_comfyui"

rm -rf "${TARGET}"
cp -R "${SOURCE}" "${TARGET}"
echo "Installed VRAM Guardian ComfyUI plugin to ${TARGET}"
