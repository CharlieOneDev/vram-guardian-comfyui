# VRAM Guardian for ComfyUI

VRAM Guardian is now a manual VRAM reservation tool for ComfyUI containers.

It runs a small Guardian process that intentionally holds CUDA memory with tensors. On startup, the default target is `VRAM_GUARDIAN_FRACTION=0.98`, meaning Guardian tries to bring total GPU memory usage up to about 98%. After that, it does not automatically release, refill, patch ComfyUI nodes, or retry OOMs. You explicitly tell it how many GiB to release and how many GiB to reserve again.

## Layout

```text
guardian/vram_guardian/
  server.py   # VRAM reservation service
  client.py   # manual control client

comfyui_plugin/vram_guardian_comfyui/
  __init__.py # ComfyUI entrypoint; automation patches are off by default

scripts/
  guardian_direct.sh # direct start/control helper for containers or hosts
```

## Recommended Container Startup

Add this to your ComfyUI container startup script:

```bash
cd /path/to/vram-guardian-comfyui
bash scripts/guardian_direct.sh start
```

Default behavior:

- Startup fills to roughly 98% total GPU usage.
- `VRAM_GUARDIAN_AUTO_REFILL=false`, so released memory is not reclaimed automatically.
- The ComfyUI plugin does not install prompt/node scheduler patches by default.
- Released VRAM goes back to the CUDA driver and can be taken by any process.

## Manual Commands

Show status:

```bash
bash scripts/guardian_direct.sh status
```

Release 8 GiB:

```bash
bash scripts/guardian_direct.sh release 8
```

Reserve 8 GiB again:

```bash
bash scripts/guardian_direct.sh reserve 8
```

Release everything currently held by Guardian:

```bash
bash scripts/guardian_direct.sh release-all
```

Fill back to the configured target, 98% by default:

```bash
bash scripts/guardian_direct.sh fill
```

Logs, stop, and restart:

```bash
bash scripts/guardian_direct.sh logs
bash scripts/guardian_direct.sh stop
bash scripts/guardian_direct.sh restart
```

## Python Client

```bash
export PYTHONPATH=/path/to/vram-guardian-comfyui/guardian:${PYTHONPATH}

python -m vram_guardian.client status --host 127.0.0.1 --port 8765
python -m vram_guardian.client release --gb 8 --host 127.0.0.1 --port 8765
python -m vram_guardian.client reserve --gb 8 --host 127.0.0.1 --port 8765
python -m vram_guardian.client fill --host 127.0.0.1 --port 8765
```

`reserve`, `occupy`, `hold`, and `allocate` are aliases. Amount-based reserve commands reserve up to the requested amount without crossing the configured target. `fill` without an amount fills to the configured target.

## Docker Compose

```bash
docker compose up -d --build
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
docker exec vram-guardian python -m vram_guardian.client release --gb 8 --host 127.0.0.1 --port 8765
docker exec vram-guardian python -m vram_guardian.client reserve --gb 8 --host 127.0.0.1 --port 8765
```

Compose defaults:

```yaml
VRAM_GUARDIAN_FRACTION: "0.98"
VRAM_GUARDIAN_MIN_FREE_MB: "0"
VRAM_GUARDIAN_AUTO_REFILL: "false"
```

## Configuration

- `VRAM_GUARDIAN_FRACTION`: target total GPU usage fraction. Default: `0.98`; values above `0.98` are capped.
- `VRAM_GUARDIAN_MIN_FREE_MB`: minimum free VRAM preserved while filling or reserving. Default: `0`.
- `VRAM_GUARDIAN_CHUNK_MB`: allocation chunk size. Default: `256`.
- `VRAM_GUARDIAN_MAX_HOLD_MB`: absolute cap for Guardian-held VRAM. `0` means no cap.
- `VRAM_GUARDIAN_DEVICE`: CUDA device. Default: `cuda:0`.
- `VRAM_GUARDIAN_HOST`: service bind address. The direct script defaults to `0.0.0.0`.
- `VRAM_GUARDIAN_PORT`: service port. Default: `8765`.
- `VRAM_GUARDIAN_AUTO_REFILL`: enables the legacy refill loop. Default: `false`.
- `VRAM_GUARDIAN_COMFYUI_AUTOMATION`: enables the legacy ComfyUI scheduler patches. Default: `false`.

## ComfyUI Plugin Behavior

By default, the ComfyUI plugin only loads and logs Guardian status. It no longer automatically:

- sets a watermark when prompts start;
- releases memory before nodes;
- releases and retries after OOM;
- reclaims memory after successful nodes.

To temporarily opt into the old automation:

```bash
export VRAM_GUARDIAN_COMFYUI_AUTOMATION=true
export VRAM_GUARDIAN_SCHEDULER_PRESET=heavy-video
export VRAM_GUARDIAN_RECLAIM_ON_SUCCESS=true
```

Manual mode usually does not need those variables.

## Caveat

Guardian is not a private VRAM pool. Released VRAM becomes normal free VRAM, so any CUDA process may take it. Guardian can only reserve memory that is still available when you run the reserve/fill command.
