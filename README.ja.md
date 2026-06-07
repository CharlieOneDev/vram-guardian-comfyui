# VRAM Guardian for ComfyUI

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

ComfyUI が OOM になったときに CUDA VRAM を解放して再試行できるようにする、実験的な sidecar です。

英語版 README がメインドキュメントです。この日本語版は概要とセットアップを素早く確認するためのものです。

これは本物の専用 VRAM プールではありません。他のプロセスが空き VRAM を先に取るのを難しくし、Guardian が保持している tensor を解放したあとに ComfyUI へ再試行の機会を与える仕組みです。

## 構成

- `guardian/`: CUDA tensor を割り当てて VRAM を保持する TCP サービス。
- `comfyui_plugin/`: `execution.get_output_data` を patch する ComfyUI `custom_nodes` プラグイン。
- `docker-compose.yml`: Linux/NVIDIA Docker 向け sidecar サービス。
- `scripts/`: プラグインを ComfyUI にコピーするための補助スクリプト。

## Guardian sidecar の起動

前提条件: Linux または WSL2 の Docker と NVIDIA Container Toolkit。

```bash
cd /path/to/vram-guardian-comfyui
docker compose up -d --build
```

状態確認:

```bash
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
```

主な環境変数:

- `VRAM_GUARDIAN_FRACTION`: 保持する GPU 総メモリの目標割合。デフォルト: `0.82`。
- `VRAM_GUARDIAN_MIN_FREE_MB`: fill 時に残す空き VRAM。デフォルト: `1536`。
- `VRAM_GUARDIAN_CHUNK_MB`: 割り当て単位。デフォルト: `256`。
- `VRAM_GUARDIAN_MAX_HOLD_MB`: 絶対的な保持上限。`0` は上限なし。
- `VRAM_GUARDIAN_DEVICE`: CUDA デバイス。デフォルト: `cuda:0`。

## ComfyUI プラグインのインストール

`comfyui_plugin/vram_guardian_comfyui` を ComfyUI の `custom_nodes` ディレクトリにコピーします。

Linux:

```bash
./scripts/install_plugin.sh /path/to/ComfyUI/custom_nodes
```

Windows PowerShell:

```powershell
.\scripts\install_plugin.ps1 -ComfyUICustomNodes "F:\path\to\ComfyUI\custom_nodes"
```

その後、以下の環境変数で ComfyUI を起動します。

```bash
VRAM_GUARDIAN_HOST=127.0.0.1 \
VRAM_GUARDIAN_PORT=8765 \
VRAM_GUARDIAN_MAX_RETRY=1 \
python main.py
```

ComfyUI が同じ compose ネットワーク上の別 Docker コンテナで動く場合:

```bash
VRAM_GUARDIAN_HOST=guardian
```

## 動作の流れ

1. Guardian は設定された目標に達するまで CUDA byte tensor を割り当てます。
2. ComfyUI プラグインが `execution.get_output_data` からの CUDA OOM を捕捉します。
3. プラグインが Guardian に VRAM の解放を依頼します。
4. プラグインが ComfyUI プロセス内の CUDA cache をクリアし、ノードを再試行します。
5. ノードが成功したあと、プラグインが Guardian に VRAM の再確保を依頼します。

## 重要な制限

- 解放された VRAM は CUDA driver に戻るだけで、ComfyUI に直接予約されるわけではありません。他のプロセスが先に取る可能性があります。
- OOM 後の再試行は、すべての custom node で安全とは限りません。副作用を持つノードがあるためです。
- 深い非同期タスク内の OOM は、解放後に再送出されるだけで、完全には再試行できない場合があります。
- 共有 GPU プラットフォームでは、大量の VRAM を長時間保持するプロセスが停止または制限される可能性があります。
- ComfyUI の workflow 自体が GPU の総 VRAM を本当に超えている場合、このツールでは解決できません。

まずは `VRAM_GUARDIAN_FRACTION=0.65` のような保守的な設定から始め、安定性を確認してから上げてください。
