# VRAM Guardian for ComfyUI

VRAM Guardian は、ComfyUI コンテナ向けの手動 VRAM 予約ツールです。

Guardian プロセスが CUDA tensor を確保して VRAM を保持します。デフォルトでは起動時に `VRAM_GUARDIAN_FRACTION=0.98`、つまり GPU 全体の使用率がおよそ 98% になるように確保します。その後は自動 release、自動 refill、ComfyUI ノードの自動 patch、OOM retry は行いません。必要な量だけ手動で解放し、必要な量だけ手動で再確保します。

## 構成

```text
guardian/vram_guardian/
  server.py   # VRAM 予約サービス
  client.py   # 手動操作クライアント

comfyui_plugin/vram_guardian_comfyui/
  __init__.py # ComfyUI エントリ。自動 patch はデフォルト無効

scripts/
  guardian_direct.sh # コンテナ/ホスト用の起動・操作スクリプト
```

## コンテナ起動時の使い方

ComfyUI コンテナの起動スクリプトに追加します。

```bash
cd /path/to/vram-guardian-comfyui
bash scripts/guardian_direct.sh start
```

デフォルト動作:

- 起動時に GPU 全体の約 98% まで確保します。
- `VRAM_GUARDIAN_AUTO_REFILL=false` のため、自動再確保はしません。
- ComfyUI plugin は prompt/node scheduler patch を自動インストールしません。
- 解放された VRAM は CUDA driver に戻り、他プロセスも使用できます。

## 手動コマンド

状態確認:

```bash
bash scripts/guardian_direct.sh status
```

8 GiB 解放:

```bash
bash scripts/guardian_direct.sh release 8
```

8 GiB 再確保:

```bash
bash scripts/guardian_direct.sh reserve 8
```

Guardian が保持している VRAM をすべて解放:

```bash
bash scripts/guardian_direct.sh release-all
```

設定された目標値、デフォルト 98%、まで再確保:

```bash
bash scripts/guardian_direct.sh fill
```

ログ、停止、再起動:

```bash
bash scripts/guardian_direct.sh logs
bash scripts/guardian_direct.sh stop
bash scripts/guardian_direct.sh restart
```

## Python クライアント

```bash
export PYTHONPATH=/path/to/vram-guardian-comfyui/guardian:${PYTHONPATH}

python -m vram_guardian.client status --host 127.0.0.1 --port 8765
python -m vram_guardian.client release --gb 8 --host 127.0.0.1 --port 8765
python -m vram_guardian.client reserve --gb 8 --host 127.0.0.1 --port 8765
python -m vram_guardian.client fill --host 127.0.0.1 --port 8765
```

`reserve`、`occupy`、`hold`、`allocate` は同義です。量を指定した再確保コマンドは、指定量を上限として、設定された目標値を超えない範囲で確保します。`fill` は量を指定しない場合、設定された目標値まで確保します。

## Docker Compose

```bash
docker compose up -d --build
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
docker exec vram-guardian python -m vram_guardian.client release --gb 8 --host 127.0.0.1 --port 8765
docker exec vram-guardian python -m vram_guardian.client reserve --gb 8 --host 127.0.0.1 --port 8765
```

Compose のデフォルト:

```yaml
VRAM_GUARDIAN_FRACTION: "0.98"
VRAM_GUARDIAN_MIN_FREE_MB: "0"
VRAM_GUARDIAN_AUTO_REFILL: "false"
```

## 主な環境変数

- `VRAM_GUARDIAN_FRACTION`: GPU 全体の目標使用率。デフォルト `0.98`。
- `VRAM_GUARDIAN_MIN_FREE_MB`: 確保時に残す最低空き VRAM。デフォルト `0`。
- `VRAM_GUARDIAN_CHUNK_MB`: 確保チャンクサイズ。デフォルト `256`。
- `VRAM_GUARDIAN_MAX_HOLD_MB`: Guardian 自身の保持上限。`0` は無制限。
- `VRAM_GUARDIAN_DEVICE`: CUDA device。デフォルト `cuda:0`。
- `VRAM_GUARDIAN_HOST`: bind address。direct script では `0.0.0.0`。
- `VRAM_GUARDIAN_PORT`: port。デフォルト `8765`。
- `VRAM_GUARDIAN_AUTO_REFILL`: 旧 refill loop を有効化。デフォルト `false`。
- `VRAM_GUARDIAN_COMFYUI_AUTOMATION`: 旧 ComfyUI scheduler patch を有効化。デフォルト `false`。

## ComfyUI plugin

デフォルトでは plugin は Guardian の status を log するだけです。prompt 開始時の watermark、node 前 release、OOM retry、成功後 reclaim は自動実行されません。

旧自動ロジックを一時的に使う場合:

```bash
export VRAM_GUARDIAN_COMFYUI_AUTOMATION=true
export VRAM_GUARDIAN_SCHEDULER_PRESET=heavy-video
export VRAM_GUARDIAN_RECLAIM_ON_SUCCESS=true
```

## 注意

Guardian は専用 VRAM pool ではありません。解放した VRAM は通常の空き VRAM になり、他の CUDA process も使用できます。Guardian は操作時点で空いている VRAM だけを再確保できます。
