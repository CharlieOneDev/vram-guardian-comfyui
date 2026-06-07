# VRAM Guardian for ComfyUI

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

Experimental sidecar for reserving CUDA VRAM and releasing it when ComfyUI hits an OOM.

This is not a real private VRAM pool. It only makes opportunistic VRAM stealing harder and gives ComfyUI one or more retry chances after the guardian releases held tensors.

## Layout

- `guardian/`: TCP service that allocates CUDA tensors to hold VRAM.
- `comfyui_plugin/`: ComfyUI `custom_nodes` plugin that patches `execution.get_output_data`.
- `docker-compose.yml`: Linux/NVIDIA Docker sidecar service.
- `scripts/`: helper scripts for copying the plugin into ComfyUI.

## Run the Guardian sidecar

Prerequisite: Docker on Linux or WSL2 with NVIDIA Container Toolkit.

```bash
cd /path/to/vram-guardian-comfyui
docker compose up -d --build
```

Check status:

```bash
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
```

Useful environment variables:

- `VRAM_GUARDIAN_FRACTION`: target fraction of total GPU memory to hold. Default: `0.82`.
- `VRAM_GUARDIAN_MIN_FREE_MB`: memory to leave free while filling. Default: `1536`.
- `VRAM_GUARDIAN_CHUNK_MB`: allocation granularity. Default: `256`.
- `VRAM_GUARDIAN_MAX_HOLD_MB`: absolute cap, `0` means no cap.
- `VRAM_GUARDIAN_DEVICE`: CUDA device. Default: `cuda:0`.

## Install the ComfyUI plugin

Copy `comfyui_plugin/vram_guardian_comfyui` into ComfyUI's `custom_nodes` directory.

Linux:

```bash
./scripts/install_plugin.sh /path/to/ComfyUI/custom_nodes
```

Windows PowerShell:

```powershell
.\scripts\install_plugin.ps1 -ComfyUICustomNodes "F:\path\to\ComfyUI\custom_nodes"
```

Then start ComfyUI with:

```bash
VRAM_GUARDIAN_HOST=127.0.0.1 \
VRAM_GUARDIAN_PORT=8765 \
VRAM_GUARDIAN_MAX_RETRY=1 \
python main.py
```

If ComfyUI runs in another Docker container on the same compose network, set:

```bash
VRAM_GUARDIAN_HOST=guardian
```

## How it behaves

1. The Guardian allocates CUDA byte tensors until the configured target is reached.
2. The ComfyUI plugin catches CUDA OOM from `execution.get_output_data`.
3. The plugin tells Guardian to release VRAM.
4. The plugin clears the ComfyUI process CUDA cache and retries the node.
5. After a successful node, the plugin asks Guardian to reclaim VRAM.

## Important limits

- Released VRAM returns to the CUDA driver, not directly to ComfyUI. Another process can still win the race.
- Retrying a node after OOM may not be safe for every custom node because some nodes have side effects.
- OOM from deeply asynchronous tasks may only be released and re-raised, not fully retried.
- Shared GPU platforms may kill or throttle a process that intentionally holds a lot of VRAM.
- This does not help if the ComfyUI workflow genuinely needs more VRAM than the GPU has.

Start conservatively, for example `VRAM_GUARDIAN_FRACTION=0.65`, then increase only after testing.
