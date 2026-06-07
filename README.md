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

Prerequisites:

- An NVIDIA GPU visible to the host. Check with `nvidia-smi`.
- Docker and Docker Compose. Check with `docker --version` and `docker compose version`.
- Docker GPU access. Check with:

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

If that command prints an `nvidia-smi` GPU table, Docker can see your GPU and this project can run. If it fails, fix Docker GPU access before starting Guardian.

On Windows, use Docker Desktop with the WSL2 backend. Docker documents GPU support for Docker Desktop on Windows with WSL2, and Docker Engine documents the `--gpus` flag for exposing NVIDIA GPUs to containers. On native Linux, install and configure NVIDIA Container Toolkit for Docker. See:

- Docker GPU access: <https://docs.docker.com/engine/containers/gpu/>
- Docker Desktop GPU support: <https://docs.docker.com/desktop/features/gpu/>
- NVIDIA Container Toolkit install guide: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>
- NVIDIA sample workload check: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html>

If NVIDIA Container Toolkit is missing on Linux, follow NVIDIA's install guide, then run:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all ubuntu nvidia-smi
```

`/path/to/vram-guardian-comfyui` means the directory that contains this README and `docker-compose.yml`.

If you have not cloned the repository yet:

```bash
git clone https://github.com/CharlieOneDev/vram-guardian-comfyui.git
cd vram-guardian-comfyui
```

If you are using the local copy created under `G:\codex`, use one of these:

```powershell
cd G:\codex\vram-guardian-comfyui
```

```bash
cd /mnt/g/codex/vram-guardian-comfyui
```

Then start Guardian:

```bash
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
