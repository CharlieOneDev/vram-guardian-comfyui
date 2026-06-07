# VRAM Guardian for ComfyUI

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

这是一个实验性的 sidecar，用于预留 CUDA 显存，并在 ComfyUI 遇到 OOM 时释放显存给它重试。

英文版 README 是主文档；这个中文版本用于快速理解和安装。

这不是一个真正的私有显存池。它只能让其他进程更难提前抢走空闲显存，并在 Guardian 释放持有的 tensor 后，给 ComfyUI 一次或多次重试机会。

## 项目结构

- `guardian/`: TCP 服务，通过分配 CUDA tensor 来占用显存。
- `comfyui_plugin/`: ComfyUI `custom_nodes` 插件，用于 patch `execution.get_output_data`。
- `docker-compose.yml`: Linux/NVIDIA Docker sidecar 服务。
- `scripts/`: 将插件复制到 ComfyUI 的辅助脚本。

## 运行 Guardian sidecar

前提条件：

- 宿主机能看到 NVIDIA GPU。用 `nvidia-smi` 检查。
- 已安装 Docker 和 Docker Compose。用 `docker --version` 和 `docker compose version` 检查。
- Docker 能访问 GPU。用下面的命令检查：

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

如果这个命令能打印 `nvidia-smi` 的 GPU 表格，说明 Docker 可以使用 GPU，这个项目就可以跑。如果失败，需要先修 Docker GPU 访问，再启动 Guardian。

Windows 上建议使用 Docker Desktop，并启用 WSL2 backend。Docker 官方文档说明，Docker Desktop for Windows 的 GPU 支持依赖 WSL2 backend；Docker Engine 文档也说明了用 `--gpus` 把 NVIDIA GPU 暴露给容器。原生 Linux 上需要安装并配置 NVIDIA Container Toolkit。参考：

- Docker GPU access: <https://docs.docker.com/engine/containers/gpu/>
- Docker Desktop GPU support: <https://docs.docker.com/desktop/features/gpu/>
- NVIDIA Container Toolkit 安装文档: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>
- NVIDIA sample workload 检查: <https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html>

如果 Linux 里还没安装 NVIDIA Container Toolkit，按 NVIDIA 官方安装文档安装后，通常还需要执行：

```bash
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all ubuntu nvidia-smi
```

`/path/to/vram-guardian-comfyui` 的意思是“这个仓库所在目录”，也就是包含本 README 和 `docker-compose.yml` 的那个目录。

如果你还没有拉取仓库，先执行：

```bash
git clone https://github.com/CharlieOneDev/vram-guardian-comfyui.git
cd vram-guardian-comfyui
```

如果你使用的是我之前创建在 `G:\codex` 下面的本地目录，在 Windows PowerShell 里是：

```powershell
cd G:\codex\vram-guardian-comfyui
```

如果你在 WSL 终端里访问同一个 Windows 盘符，通常是：

```bash
cd /mnt/g/codex/vram-guardian-comfyui
```

然后启动 Guardian：

```bash
docker compose up -d --build
```

如果你的 Compose 实现不认识 GPU 字段，报类似 `Additional property gpus is not allowed`，说明 Docker 本身可能能用 GPU，但 compose 文件的 GPU 语法太新。可以改用下面这个直接 Docker 启动方式：

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

如果直接 Docker 启动也在 NVIDIA runtime 初始化阶段失败，并出现类似 `/proc/driver/nvidia/gpus/... no such file or directory`，说明当前 CNB/嵌套 Docker 环境不适合再套一层 GPU 容器。这时改用 direct 模式：直接在当前 Linux/CNB/WSL 环境里跑 Guardian 进程。

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
VRAM_GUARDIAN_FRACTION=0.82 bash scripts/guardian_direct.sh start
```

direct 模式会把日志写到 `vram_guardian.log`，PID 写到 `vram_guardian.pid`。默认会启用 auto-refill，所以 Guardian 会定期检查新释放出来的显存，并尽量补占回配置目标。

```bash
bash scripts/guardian_direct.sh status
bash scripts/guardian_direct.sh logs
bash scripts/guardian_direct.sh stop
```

实时日志会显示一行显存摘要：

```text
total=45458MiB free=1536MiB guardian_held=32656MiB target=39086MiB external_calc=11266MiB guardian_proc=33126MiB comfyui=2048MiB other=9218MiB paused=0s
```

Docker 或 Compose 模式用下面的命令查看状态：

```bash
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
```

常用环境变量：

- `VRAM_GUARDIAN_FRACTION`: 目标占用的 GPU 总显存比例。默认：`0.82`。
- `VRAM_GUARDIAN_MIN_FREE_MB`: 填充显存时保留的空闲显存。默认：`1536`。
- `VRAM_GUARDIAN_CHUNK_MB`: 分配粒度。默认：`256`。
- `VRAM_GUARDIAN_MAX_HOLD_MB`: 绝对占用上限，`0` 表示不设置上限。
- `VRAM_GUARDIAN_DEVICE`: CUDA 设备。默认：`cuda:0`。
- `VRAM_GUARDIAN_AUTO_REFILL`: 定期自动补占新释放出来的显存。默认：`true`。
- `VRAM_GUARDIAN_AUTO_REFILL_INTERVAL_SEC`: 自动补占检查间隔。默认：`5`。
- `VRAM_GUARDIAN_AUTO_REFILL_MIN_DELTA_MB`: 至少多出多少可占用显存才触发自动补占。默认：`256`。
- `VRAM_GUARDIAN_RELEASE_BEFORE_NODE`: ComfyUI 插件在每个节点执行前先释放 Guardian。默认：`false`。
- `VRAM_GUARDIAN_RELEASE_REFILL_PAUSE_SEC`: release 后暂停 auto-refill，给 ComfyUI 留出分配时间。默认：`3600`。
- `VRAM_GUARDIAN_COMFYUI_PID`: 可选，用于自动识别不准时，指定 ComfyUI PID 以便日志区分 ComfyUI 显存。
- `VRAM_GUARDIAN_ACTIVE_FREE_MB`: 插件在匹配节点运行期间开启 Guardian watermark 模式，并维持这么多空闲显存。默认：`0`，表示关闭。
- `VRAM_GUARDIAN_ACTIVE_HYSTERESIS_MB`: 空闲显存超过目标多少后，Guardian 才补占多余部分。默认：`2048`。
- `VRAM_GUARDIAN_HEAVY_NODES`: 逗号分隔的重节点 class 名称。为空时，只要设置了 `ACTIVE_FREE_MB` 就对所有节点生效。
- `VRAM_GUARDIAN_WATERMARK_INTERVAL_SEC`: Guardian 在 watermark 模式下的检查间隔。默认：`1`。

## 安装 ComfyUI 插件

将 `comfyui_plugin/vram_guardian_comfyui` 复制到 ComfyUI 的 `custom_nodes` 目录。

Linux：

```bash
./scripts/install_plugin.sh /path/to/ComfyUI/custom_nodes
```

Windows PowerShell：

```powershell
.\scripts\install_plugin.ps1 -ComfyUICustomNodes "F:\path\to\ComfyUI\custom_nodes"
```

然后用以下环境变量启动 ComfyUI：

```bash
VRAM_GUARDIAN_HOST=127.0.0.1 \
VRAM_GUARDIAN_PORT=8765 \
VRAM_GUARDIAN_MAX_RETRY=1 \
python main.py
```

如果你的工作流一开始就需要大量显存，不想等第一次 OOM 后再释放，可以让插件在每个节点执行前先释放 Guardian：

```bash
VRAM_GUARDIAN_HOST=127.0.0.1 \
VRAM_GUARDIAN_PORT=8765 \
VRAM_GUARDIAN_MAX_RETRY=1 \
VRAM_GUARDIAN_RELEASE_BEFORE_NODE=1 \
VRAM_GUARDIAN_RELEASE_REFILL_PAUSE_SEC=3600 \
python main.py
```

对于长时间运行的重节点，更推荐 active watermark 模式。Guardian 会在节点执行期间维持一段目标空闲显存，同时继续占住多余显存作为防抢占保护：

```bash
VRAM_GUARDIAN_HOST=127.0.0.1 \
VRAM_GUARDIAN_PORT=8765 \
VRAM_GUARDIAN_HEAVY_NODES=SegmentVSRFIStreamRunner \
VRAM_GUARDIAN_ACTIVE_FREE_MB=20480 \
VRAM_GUARDIAN_ACTIVE_HYSTERESIS_MB=2048 \
VRAM_GUARDIAN_RELEASE_BEFORE_NODE=0 \
VRAM_GUARDIAN_RECLAIM_ON_SUCCESS=1 \
python main.py
```

这个模式下，如果空闲显存低于 `ACTIVE_FREE_MB`，Guardian 会动态释放 chunk；如果空闲显存高于 `ACTIVE_FREE_MB + ACTIVE_HYSTERESIS_MB`，Guardian 才会补占多余部分。

如果 ComfyUI 运行在同一个 compose 网络里的另一个 Docker 容器中，设置：

```bash
VRAM_GUARDIAN_HOST=guardian
```

## 工作方式

1. Guardian 分配 CUDA byte tensor，直到达到配置的目标占用。
2. ComfyUI 插件捕获来自 `execution.get_output_data` 的 CUDA OOM。
3. 插件通知 Guardian 释放显存。
4. 插件清理 ComfyUI 进程内的 CUDA cache，然后重试节点。
5. 节点成功后，插件通知 Guardian 重新占回显存。

## 重要限制

- 释放的显存会回到 CUDA driver，不会定向分配给 ComfyUI；其他进程仍可能抢先拿走。
- OOM 后重试节点不一定适用于所有自定义节点，因为某些节点可能有副作用。
- 深层异步任务中的 OOM 可能只能触发释放并重新抛出，不能完整重试。
- 共享 GPU 平台可能会杀掉或限制故意长期占用大量显存的进程。
- 如果 ComfyUI 工作流本身真的超过 GPU 总显存，这个工具无法解决。

建议从保守配置开始，例如 `VRAM_GUARDIAN_FRACTION=0.65`，测试稳定后再提高。
