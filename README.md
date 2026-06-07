# VRAM Guardian for ComfyUI

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

VRAM Guardian for ComfyUI is an experimental VRAM reservation and scheduling helper for shared NVIDIA GPU environments.

It runs a small Guardian process that intentionally holds CUDA memory, plus an optional ComfyUI plugin that coordinates when the Guardian should release or reclaim that memory. The goal is to reduce opportunistic VRAM stealing by other processes while still giving ComfyUI room before and during heavy workflow steps.

## What This Is

The project has two parts:

- `guardian/`: a TCP service that allocates CUDA tensors to reserve VRAM and exposes commands such as `status`, `release`, `ensure_free`, `set_watermark`, and `reclaim`.
- `comfyui_plugin/`: a ComfyUI `custom_nodes` plugin that monkey-patches ComfyUI execution so it can schedule VRAM before nodes run and retry once after CUDA OOM.

This is not a private VRAM pool. Released VRAM returns to the CUDA driver, so another process may still take it. The scheduler reduces the race window; it cannot provide hard isolation.

## Scheduling Model

Guardian can run in simple reservation mode or in Scheduler mode.

Simple mode:

1. Guardian fills VRAM up to `VRAM_GUARDIAN_FRACTION`.
2. The ComfyUI plugin releases Guardian memory on OOM.
3. Guardian reclaims memory after successful execution.

Scheduler mode:

1. A workflow starts with an automatic base free-VRAM target.
2. Before a heavy video, sampler, model, VAE, pose, upscale, interpolation, or VSR node runs, the plugin raises the free-VRAM target.
3. Guardian releases enough held memory before the node starts.
4. The plugin waits until the target free VRAM is reached, or until a configurable timeout.
5. During the node, Guardian keeps the higher target and only refills surplus above `target + hysteresis`.
6. After the node, the high target is removed and Guardian returns to the base target.
7. If a node OOMs, Guardian fully releases, ComfyUI clears CUDA cache, the node retries once, and the profile can bump the next target.

## Important Limits

- This does not create a private ComfyUI memory pool.
- Released VRAM is visible to every process on the GPU.
- The plugin cannot intercept every low-level `cudaMalloc` at the exact allocation moment.
- Node retry after OOM may not be safe for every custom node if that node has external side effects.
- If another process already owns the required VRAM, Guardian can wait and warn, but it cannot force that process to release memory.
- This cannot make workflows fit on a GPU if the workflow genuinely needs more VRAM than the card has.

## Repository Layout

```text
guardian/                       Guardian TCP server and CLI client
comfyui_plugin/vram_guardian_comfyui/
                                ComfyUI custom node plugin
scripts/                        Install and direct-run helper scripts
docker-compose.yml              Optional Docker sidecar entry point
Dockerfile                      Optional Guardian container image
```

## Start Guardian

Choose one of the following deployment modes.

### Option A: Docker Compose Sidecar

Use this when Docker can directly access the NVIDIA GPU.

Prerequisites:

```bash
nvidia-smi
docker --version
docker compose version
docker run --rm --gpus all ubuntu nvidia-smi
```

Clone and start:

```bash
git clone https://github.com/CharlieOneDev/vram-guardian-comfyui.git
cd vram-guardian-comfyui
docker compose up -d --build
```

Check Guardian:

```bash
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
```

If Docker reports `Additional property gpus is not allowed`, your Compose implementation is too old for that syntax. Use direct Docker or direct Python mode instead.

### Option B: Direct Docker

```bash
docker build -t vram-guardian-comfyui:local .
docker rm -f vram-guardian 2>/dev/null || true
docker run -d \
  --name vram-guardian \
  --restart unless-stopped \
  --gpus all \
  -p 127.0.0.1:8765:8765 \
  -e VRAM_GUARDIAN_HOST=0.0.0.0 \
  -e VRAM_GUARDIAN_PORT=8765 \
  -e VRAM_GUARDIAN_DEVICE=cuda:0 \
  -e VRAM_GUARDIAN_FRACTION=0.82 \
  vram-guardian-comfyui:local
```

If Docker fails during NVIDIA runtime initialization, run Guardian directly in the same Linux, WSL2, or cloud container environment as ComfyUI.

### Option C: Direct Python

Use this when nested GPU Docker is unavailable or unreliable.

```bash
cd vram-guardian-comfyui
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
VRAM_GUARDIAN_FRACTION=0.82 bash scripts/guardian_direct.sh start
```

Common direct-mode commands:

```bash
bash scripts/guardian_direct.sh status
bash scripts/guardian_direct.sh logs
bash scripts/guardian_direct.sh stop
```

Direct mode writes:

```text
vram_guardian.log
vram_guardian.pid
```

## Install the ComfyUI Plugin

Copy the plugin into ComfyUI's `custom_nodes` directory.

Linux:

```bash
cd vram-guardian-comfyui
./scripts/install_plugin.sh /path/to/ComfyUI/custom_nodes
```

Windows PowerShell:

```powershell
cd vram-guardian-comfyui
.\scripts\install_plugin.ps1 -ComfyUICustomNodes "D:\path\to\ComfyUI\custom_nodes"
```

Restart ComfyUI after installing or updating the plugin.

## Recommended Scheduler Configuration

The ComfyUI plugin now defaults to the `heavy-video` scheduler preset. This preset is designed for workflows that load video frames and reference images, run pose/control preprocessing, generate with Wan/LTX-style video models, then decode, interpolate, upscale, or VSR the result.

With Guardian already running, the ComfyUI side can usually start with only:

```bash
export VRAM_GUARDIAN_HOST=127.0.0.1
export VRAM_GUARDIAN_PORT=8765
export VRAM_GUARDIAN_MAX_RETRY=1

python main.py
```

Default `heavy-video` behavior:

- Scheduler mode is enabled by default.
- Base workflow free target is automatic: `min(total_vram * 0.62, 28672 MiB)`.
- Heavy node free target is automatic: `min(total_vram * 0.72, 32768 MiB)`.
- Local profiling is enabled by default and writes `vram_guardian_profile.json`.
- Heavy nodes are detected by broad class-name patterns such as `sampler`, `wan`, `ltx`, `bernini`, `video`, `vsr`, `upscale`, `interpol`, `decode`, `vae`, `pose`, and `model`.

For a 48 GiB/L40-class GPU this means roughly:

```text
base free target:  about 28 GiB
heavy free target: about 32 GiB
```

Manual overrides are still available:

```bash
export VRAM_GUARDIAN_BASE_FREE_MB=32768
export VRAM_GUARDIAN_HEAVY_FREE_MB=36864
export VRAM_GUARDIAN_HEAVY_PATTERNS=sampler,wan,ltx,bernini,video,vsr,upscale,interpol,decode,vae,pose,model
python main.py
```

Set `VRAM_GUARDIAN_SCHEDULER_PRESET=manual` if you want the old behavior where scheduler targets are only enabled by explicit variables.

For Windows PowerShell:

```powershell
$env:VRAM_GUARDIAN_HOST = "127.0.0.1"
$env:VRAM_GUARDIAN_PORT = "8765"
python main.py
```

## Scheduler Environment Variables

Guardian process:

- `VRAM_GUARDIAN_FRACTION`: target fraction of total GPU memory to hold. Default: `0.82`.
- `VRAM_GUARDIAN_MIN_FREE_MB`: minimum free memory while Guardian fills. Default: `1536`.
- `VRAM_GUARDIAN_CHUNK_MB`: allocation chunk size. Default: `256`.
- `VRAM_GUARDIAN_MAX_HOLD_MB`: absolute hold cap. `0` means no cap.
- `VRAM_GUARDIAN_DEVICE`: CUDA device. Default: `cuda:0`.
- `VRAM_GUARDIAN_AUTO_REFILL`: enable control loop. Default: `true`.
- `VRAM_GUARDIAN_AUTO_REFILL_INTERVAL_SEC`: normal refill interval. Default: `5`.
- `VRAM_GUARDIAN_AUTO_REFILL_MIN_DELTA_MB`: minimum allocation delta. Default: `256`.
- `VRAM_GUARDIAN_WATERMARK_INTERVAL_SEC`: active watermark loop interval. Default: `1`.
- `VRAM_GUARDIAN_WATERMARK_RELEASE_COOLDOWN_SEC`: delay before refilling after a watermark release. Default: `5`.

ComfyUI plugin:

- `VRAM_GUARDIAN_SCHEDULER_PRESET`: preset name. Default: `heavy-video`. Use `manual` or `off` to disable automatic heavy-video defaults.
- `VRAM_GUARDIAN_SCHEDULER_ENABLE`: enable Scheduler mode. Default: `true` under the `heavy-video` preset.
- `VRAM_GUARDIAN_BASE_FREE_MB`: explicit base free-VRAM target. If unset, `heavy-video` uses `min(total_vram * 0.62, 28672 MiB)`.
- `VRAM_GUARDIAN_HEAVY_FREE_MB`: explicit heavy-node target. If unset, `heavy-video` uses `min(total_vram * 0.72, 32768 MiB)`.
- `VRAM_GUARDIAN_AUTO_BASE_FREE_FRACTION`: automatic base target fraction. Default: `0.62`.
- `VRAM_GUARDIAN_AUTO_HEAVY_FREE_FRACTION`: automatic heavy target fraction. Default: `0.72`.
- `VRAM_GUARDIAN_AUTO_BASE_FREE_CAP_MB`: automatic base target cap. Default: `28672`.
- `VRAM_GUARDIAN_AUTO_HEAVY_FREE_CAP_MB`: automatic heavy target cap. Default: `32768`.
- `VRAM_GUARDIAN_NODE_FREE_MAP`: JSON object mapping node class names to target free MiB.
- `VRAM_GUARDIAN_HEAVY_NODES`: comma-separated heavy node class names using `HEAVY_FREE_MB`.
- `VRAM_GUARDIAN_HEAVY_PATTERNS`: comma-separated lowercase class-name substrings treated as heavy nodes. Defaults to a video-oriented set under `heavy-video`.
- `VRAM_GUARDIAN_ACTIVE_HYSTERESIS_MB`: surplus band before Guardian refills during a high target.
- `VRAM_GUARDIAN_WAIT_TIMEOUT_SEC`: maximum wait before a node starts. `0` means wait indefinitely.
- `VRAM_GUARDIAN_WAIT_POLL_SEC`: wait-loop polling interval.
- `VRAM_GUARDIAN_WAIT_LOG_INTERVAL_SEC`: repeated wait-log interval.
- `VRAM_GUARDIAN_MONITOR_INTERVAL_SEC`: node runtime sampling interval for profiling.
- `VRAM_GUARDIAN_PROFILE_ENABLE`: write local profile data. Default: `true` under `heavy-video`.
- `VRAM_GUARDIAN_PROFILE_PATH`: profile JSON path. Default: `vram_guardian_profile.json`.
- `VRAM_GUARDIAN_PROFILE_MARGIN_MB`: extra margin added to learned targets.
- `VRAM_GUARDIAN_OOM_BUMP_MB`: target increase after OOM.
- `VRAM_GUARDIAN_MAX_RETRY`: node retry count after OOM. Default: `1`.

Legacy plugin controls remain available:

- `VRAM_GUARDIAN_RELEASE_BEFORE_NODE`
- `VRAM_GUARDIAN_RELEASE_REFILL_PAUSE_SEC`
- `VRAM_GUARDIAN_ACTIVE_SCOPE`
- `VRAM_GUARDIAN_ACTIVE_FREE_MB`
- `VRAM_GUARDIAN_RECLAIM_ON_SUCCESS`

## Logs and Verification

Guardian logs include compact VRAM summaries:

```text
total=45458MiB free=28672MiB guardian_held=9216MiB target=37275MiB external_calc=7570MiB guardian_proc=9472MiB comfyui=4096MiB other=3474MiB paused=0s
```

Scheduler logs use the `[VRAM Scheduler]` prefix:

```text
[VRAM Scheduler] node=WanVideoSampler#12 class=WanVideoSampler target_free=32768MiB source=heavy-pattern
[VRAM Scheduler] WanVideoSampler#12 waiting: free=28672MiB target=32768MiB guardian_held=4096MiB
[VRAM Scheduler] WanVideoSampler#12 free reached 32800MiB target=32768MiB; continuing
```

If Guardian has released all held memory and the target is still not available:

```text
[VRAM Scheduler] WanVideoSampler#12 Guardian holds no VRAM but free is still below target; another process may be using the GPU
```

## Tuning Notes

- Increase `BASE_FREE_MB` or `HEAVY_FREE_MB` if heavy video workflows still OOM.
- Add a node to `NODE_FREE_MAP` only when logs show a specific class needs a custom target.
- Lower `BASE_FREE_MB`, `HEAVY_FREE_MB`, or the automatic caps if protection against other users is more important than latency.
- Lower `WAIT_TIMEOUT_SEC` if workloads should fail fast when the GPU is already occupied.
- Enable profiling only when you want the plugin to write local learning data.

Start conservatively, observe logs, then raise `VRAM_GUARDIAN_FRACTION` or lower free targets after the workflow is stable.
