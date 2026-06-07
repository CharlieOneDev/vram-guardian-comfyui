# VRAM Guardian for ComfyUI

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

VRAM Guardian for ComfyUI は、共有 NVIDIA GPU 環境向けの実験的な VRAM 予約・スケジューリング補助ツールです。

Guardian プロセスが CUDA tensor で VRAM を保持し、ComfyUI プラグインが workflow と node の実行タイミングに合わせて release、wait、refill を指示します。目的は、他プロセスによる VRAM の奪取をできるだけ抑えつつ、ComfyUI の重い node に必要な一時的な空き VRAM を確保することです。

## 構成

- `guardian/`: VRAM を保持する TCP service と CLI client。
- `comfyui_plugin/`: ComfyUI の `custom_nodes` plugin。実行処理を monkey-patch して VRAM scheduling を行います。
- `scripts/`: plugin install と direct run 用 helper。
- `docker-compose.yml`: optional Docker sidecar。
- `Dockerfile`: optional Guardian container image。

## 重要な制限

このツールは ComfyUI 専用の private VRAM pool を作るものではありません。Guardian が release した VRAM は CUDA driver に戻るため、他プロセスが取得する可能性は残ります。

できること:

- node 開始前に release と wait を行う。
- node 実行中に target free VRAM watermark を維持する。
- node 終了後に base watermark に戻す。
- OOM 後に release、CUDA cache cleanup、retry を行う。

できないこと:

- 任意の低レベル `cudaMalloc` を瞬間的に完全制御する。
- すでに他プロセスが占有している VRAM を強制的に取り戻す。
- GPU 容量を超える workflow を成功させる。

## Guardian の起動

### Docker Compose

Docker が NVIDIA GPU に直接アクセスできる場合:

```bash
nvidia-smi
docker --version
docker compose version
docker run --rm --gpus all ubuntu nvidia-smi
```

```bash
git clone https://github.com/CharlieOneDev/vram-guardian-comfyui.git
cd vram-guardian-comfyui
docker compose up -d --build
```

状態確認:

```bash
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
```

### Direct Docker

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

### Direct Python

Nested GPU Docker が使えない環境では、ComfyUI と同じ Linux/WSL2/cloud container で直接起動します。

```bash
cd vram-guardian-comfyui
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
VRAM_GUARDIAN_FRACTION=0.82 bash scripts/guardian_direct.sh start
```

```bash
bash scripts/guardian_direct.sh status
bash scripts/guardian_direct.sh logs
bash scripts/guardian_direct.sh stop
```

## ComfyUI プラグインのインストール

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

インストールまたは更新後、ComfyUI を再起動してください。

## 推奨 Scheduler 設定

ComfyUI plugin はデフォルトで `heavy-video` scheduler preset を使います。この preset は、video frame、reference image、pose/control preprocessing、Wan/LTX 系 video model、decode、interpolation、upscale、VSR を含む重い video workflow 向けです。

Guardian がすでに起動している場合、ComfyUI 側は通常これだけで開始できます:

```bash
export VRAM_GUARDIAN_HOST=127.0.0.1
export VRAM_GUARDIAN_PORT=8765
export VRAM_GUARDIAN_MAX_RETRY=1

python main.py
```

Default `heavy-video` behavior:

- Scheduler mode はデフォルトで有効。
- base workflow target は自動計算: `min(total_vram * 0.62, 28672 MiB)`。
- heavy node target は自動計算: `min(total_vram * 0.72, 32768 MiB)`。
- local profiling はデフォルトで有効で、`vram_guardian_profile.json` に書き込みます。
- heavy node は `sampler`, `wan`, `ltx`, `bernini`, `video`, `vsr`, `upscale`, `interpol`, `decode`, `vae`, `pose`, `model` などの class-name pattern で検出されます。

48 GiB/L40 class GPU ではおおよそ:

```text
base free target:  about 28 GiB
heavy free target: about 32 GiB
```

Manual override:

```bash
export VRAM_GUARDIAN_BASE_FREE_MB=32768
export VRAM_GUARDIAN_HEAVY_FREE_MB=36864
export VRAM_GUARDIAN_HEAVY_PATTERNS=sampler,wan,ltx,bernini,video,vsr,upscale,interpol,decode,vae,pose,model
python main.py
```

以前のように明示設定だけで scheduler target を有効にしたい場合:

```bash
export VRAM_GUARDIAN_SCHEDULER_PRESET=manual
```

## Scheduler の流れ

1. ComfyUI prompt 開始時、plugin が base watermark を開きます。
2. 通常 node は `BASE_FREE_MB` を基準に実行されます。
3. `NODE_FREE_MAP` に一致する node は、その target free VRAM まで Guardian を release します。
4. `HEAVY_NODES` または `HEAVY_PATTERNS` に一致する node は `HEAVY_FREE_MB` を使用します。
5. node 実行前、plugin は `ensure_free` を呼び、target に届くまで wait します。
6. node 実行中、Guardian は高い watermark を維持します。
7. node 終了後、高い watermark を閉じ、base watermark に戻します。
8. prompt 終了後、Guardian は通常の予約状態に戻ります。

## 主要な環境変数

Guardian:

- `VRAM_GUARDIAN_FRACTION`: GPU memory の保持割合。デフォルト `0.82`。
- `VRAM_GUARDIAN_MIN_FREE_MB`: 通常 refill 時に残す free VRAM。
- `VRAM_GUARDIAN_CHUNK_MB`: allocation chunk。
- `VRAM_GUARDIAN_MAX_HOLD_MB`: 最大保持量。
- `VRAM_GUARDIAN_WATERMARK_INTERVAL_SEC`: watermark loop interval。
- `VRAM_GUARDIAN_WATERMARK_RELEASE_COOLDOWN_SEC`: release 後の refill cooldown。

ComfyUI plugin:

- `VRAM_GUARDIAN_SCHEDULER_PRESET`: scheduler preset。デフォルト `heavy-video`。`manual` または `off` で automatic heavy-video defaults を無効化。
- `VRAM_GUARDIAN_SCHEDULER_ENABLE`: Scheduler を有効化。`heavy-video` ではデフォルト有効。
- `VRAM_GUARDIAN_BASE_FREE_MB`: explicit base target。未設定時、`heavy-video` は `min(total_vram * 0.62, 28672 MiB)` を使用。
- `VRAM_GUARDIAN_HEAVY_FREE_MB`: explicit heavy target。未設定時、`heavy-video` は `min(total_vram * 0.72, 32768 MiB)` を使用。
- `VRAM_GUARDIAN_AUTO_BASE_FREE_FRACTION`: automatic base fraction。デフォルト `0.62`。
- `VRAM_GUARDIAN_AUTO_HEAVY_FREE_FRACTION`: automatic heavy fraction。デフォルト `0.72`。
- `VRAM_GUARDIAN_AUTO_BASE_FREE_CAP_MB`: automatic base cap。デフォルト `28672`。
- `VRAM_GUARDIAN_AUTO_HEAVY_FREE_CAP_MB`: automatic heavy cap。デフォルト `32768`。
- `VRAM_GUARDIAN_NODE_FREE_MAP`: node class と target free MiB の JSON map。
- `VRAM_GUARDIAN_HEAVY_NODES`: comma-separated heavy node class names。
- `VRAM_GUARDIAN_HEAVY_PATTERNS`: comma-separated lowercase class-name substrings。`heavy-video` では video workflow 用 pattern がデフォルト。
- `VRAM_GUARDIAN_ACTIVE_HYSTERESIS_MB`: refill hysteresis。
- `VRAM_GUARDIAN_WAIT_TIMEOUT_SEC`: node 開始前の最大 wait。
- `VRAM_GUARDIAN_MONITOR_INTERVAL_SEC`: runtime sampling interval。
- `VRAM_GUARDIAN_PROFILE_ENABLE`: local profile JSON を書き込む。`heavy-video` ではデフォルト有効。
- `VRAM_GUARDIAN_PROFILE_MARGIN_MB`: learned target の margin。
- `VRAM_GUARDIAN_OOM_BUMP_MB`: OOM 後に target を増やす量。

## ログ例

Guardian summary:

```text
total=45458MiB free=28672MiB guardian_held=9216MiB target=37275MiB external_calc=7570MiB guardian_proc=9472MiB comfyui=4096MiB other=3474MiB paused=0s
```

Scheduler:

```text
[VRAM Scheduler] node=WanVideoSampler#12 class=WanVideoSampler target_free=32768MiB source=heavy-pattern
[VRAM Scheduler] WanVideoSampler#12 waiting: free=28672MiB target=32768MiB guardian_held=4096MiB
[VRAM Scheduler] WanVideoSampler#12 free reached 32800MiB target=32768MiB; continuing
```

Guardian がすべて release しても target に届かない場合:

```text
[VRAM Scheduler] WanVideoSampler#12 Guardian holds no VRAM but free is still below target; another process may be using the GPU
```

## Tuning

- heavy video workflow が OOM する場合は `BASE_FREE_MB` または `HEAVY_FREE_MB` を上げます。
- 特定 class が繰り返し OOM する場合のみ `NODE_FREE_MAP` に個別 target を追加します。
- 他プロセスへの防御を強めたい場合は `BASE_FREE_MB`、`HEAVY_FREE_MB`、または automatic cap を下げます。
- GPU が混雑しているときに早く失敗させたい場合は `WAIT_TIMEOUT_SEC` を下げます。
- 実行履歴から target を学習させたい場合は `PROFILE_ENABLE` を有効化します。
