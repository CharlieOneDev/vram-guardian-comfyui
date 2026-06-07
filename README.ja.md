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

前提条件:

- ホストから NVIDIA GPU が見えること。`nvidia-smi` で確認します。
- Docker と Docker Compose が入っていること。`docker --version` と `docker compose version` で確認します。
- Docker から GPU にアクセスできること。次のコマンドで確認します。

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

このコマンドで `nvidia-smi` の GPU テーブルが表示されれば、Docker から GPU を利用できます。失敗する場合は、Guardian を起動する前に Docker の GPU アクセスを直してください。

Windows では Docker Desktop の WSL2 backend を使ってください。Docker の公式ドキュメントでは、Docker Desktop for Windows の GPU support は WSL2 backend で利用できると説明されています。また Docker Engine のドキュメントでは、`--gpus` flag で NVIDIA GPU をコンテナへ公開する方法が説明されています。ネイティブ Linux では NVIDIA Container Toolkit をインストールして Docker に設定します。参考:

- Docker GPU access: <https://docs.docker.com/engine/containers/gpu/>
- Docker Desktop GPU support: <https://docs.docker.com/desktop/features/gpu/>
- NVIDIA Container Toolkit install guide: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>
- NVIDIA sample workload check: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html>

Linux で NVIDIA Container Toolkit が未インストールの場合は、NVIDIA の公式ガイドに従ってインストールしたあと、通常は次を実行します。

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all ubuntu nvidia-smi
```

`/path/to/vram-guardian-comfyui` は、この README と `docker-compose.yml` があるリポジトリのディレクトリを指します。

まだリポジトリを clone していない場合:

```bash
git clone https://github.com/CharlieOneDev/vram-guardian-comfyui.git
cd vram-guardian-comfyui
```

以前作成した `G:\codex` 下のローカルコピーを使う場合、Windows PowerShell では:

```powershell
cd G:\codex\vram-guardian-comfyui
```

WSL terminal から同じ Windows drive を使う場合は、通常:

```bash
cd /mnt/g/codex/vram-guardian-comfyui
```

その後 Guardian を起動します。

```bash
docker compose up -d --build
```

Compose implementation が GPU syntax を認識せず、`Additional property gpus is not allowed` のようなエラーを出す場合があります。その場合、Docker 自体は GPU を使えていても、compose file の GPU syntax が新しすぎる可能性があります。次の直接 Docker 起動を使ってください。

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
  -e VRAM_GUARDIAN_MIN_FREE_MB=1536 \
  -e VRAM_GUARDIAN_CHUNK_MB=256 \
  -e VRAM_GUARDIAN_MAX_HOLD_MB=0 \
  -e VRAM_GUARDIAN_AUTO_REFILL=true \
  -e VRAM_GUARDIAN_AUTO_REFILL_INTERVAL_SEC=5 \
  -e VRAM_GUARDIAN_AUTO_REFILL_MIN_DELTA_MB=256 \
  vram-guardian-comfyui:local
```

直接 Docker 起動でも NVIDIA runtime initialization の段階で `/proc/driver/nvidia/gpus/... no such file or directory` のようなエラーが出る場合は、現在の CNB/nested Docker 環境では GPU コンテナをさらに重ねるのが合っていません。その場合は direct mode を使い、現在の Linux/CNB/WSL 環境で Guardian プロセスを直接起動します。

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
VRAM_GUARDIAN_FRACTION=0.82 bash scripts/guardian_direct.sh start
```

direct mode は log を `vram_guardian.log` に、PID を `vram_guardian.pid` に書きます。auto-refill はデフォルトで有効なので、Guardian は新しく空いた VRAM を定期的に確認し、設定された target まで再確保します。

```bash
bash scripts/guardian_direct.sh status
bash scripts/guardian_direct.sh logs
bash scripts/guardian_direct.sh stop
```

Docker または Compose mode の状態確認:

```bash
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
```

主な環境変数:

- `VRAM_GUARDIAN_FRACTION`: 保持する GPU 総メモリの目標割合。デフォルト: `0.82`。
- `VRAM_GUARDIAN_MIN_FREE_MB`: fill 時に残す空き VRAM。デフォルト: `1536`。
- `VRAM_GUARDIAN_CHUNK_MB`: 割り当て単位。デフォルト: `256`。
- `VRAM_GUARDIAN_MAX_HOLD_MB`: 絶対的な保持上限。`0` は上限なし。
- `VRAM_GUARDIAN_DEVICE`: CUDA デバイス。デフォルト: `cuda:0`。
- `VRAM_GUARDIAN_AUTO_REFILL`: 新しく空いた VRAM を定期的に再確保します。デフォルト: `true`。
- `VRAM_GUARDIAN_AUTO_REFILL_INTERVAL_SEC`: auto-refill の確認間隔。デフォルト: `5`。
- `VRAM_GUARDIAN_AUTO_REFILL_MIN_DELTA_MB`: auto-refill が割り当てを始める最小増分。デフォルト: `256`。
- `VRAM_GUARDIAN_RELEASE_BEFORE_NODE`: ComfyUI plugin が各 node の実行前に Guardian を解放します。デフォルト: `false`。
- `VRAM_GUARDIAN_RELEASE_REFILL_PAUSE_SEC`: release 後に auto-refill を一時停止し、ComfyUI が割り当てる時間を確保します。デフォルト: `3600`。

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

Workflow が最初から大量の VRAM を必要とする場合は、最初の OOM を待たずに、各 node の実行前に Guardian を解放できます。

```bash
VRAM_GUARDIAN_HOST=127.0.0.1 \
VRAM_GUARDIAN_PORT=8765 \
VRAM_GUARDIAN_MAX_RETRY=1 \
VRAM_GUARDIAN_RELEASE_BEFORE_NODE=1 \
VRAM_GUARDIAN_RELEASE_REFILL_PAUSE_SEC=3600 \
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
