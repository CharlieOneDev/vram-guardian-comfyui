# ComfyUI VRAM Guardian

[English](README.md) | [简体中文](README.zh-CN.md) | [日本語](README.ja.md)

VRAM Guardian for ComfyUI 是一个实验性的显存保留与调度工具，适合多人共享 NVIDIA GPU 的环境。

它由两部分组成：一个 Guardian 进程负责预占 CUDA 显存，一个 ComfyUI 插件负责在工作流和节点执行阶段通知 Guardian 释放、等待或回填显存。目标是在尽量防止其他进程抢占显存的同时，给 ComfyUI 的重节点保留突发申请空间。

## 核心能力

- Guardian 通过 CUDA tensor 预占显存。
- ComfyUI 插件 monkey-patch 执行流程，在节点执行前做显存调度。
- 普通阶段维持较低空闲显存，减少被其他进程抢走的窗口。
- 重节点执行前提高目标空闲显存，并等待 Guardian 释放到位。
- 重节点执行中保持高水位，避免中途回填过猛导致后半段 OOM。
- 节点结束后恢复基础水位并回填。
- OOM 后全释放、清理 CUDA cache、重试一次，并可提高该节点下次预算。
- 可选本地 profiling，记录节点显存行为。

## 重要限制

这个工具不能创建 ComfyUI 私有显存池。Guardian 释放的显存会回到 CUDA driver，其他进程仍然可能抢走。

插件可以做到：

- 节点开始前释放和等待；
- 节点运行中维持水位；
- 节点结束后回填；
- OOM 后释放和重试。

插件很难做到：

- 拦截任意底层 `cudaMalloc` 的瞬间申请；
- 在其他进程已经占满显存时强制拿回显存；
- 让超出 GPU 总显存的工作流成功运行。

## 项目结构

```text
guardian/                       Guardian TCP 服务端和 CLI 客户端
comfyui_plugin/vram_guardian_comfyui/
                                ComfyUI custom_nodes 插件
scripts/                        安装插件和直接运行 Guardian 的脚本
docker-compose.yml              可选 Docker sidecar 启动方式
Dockerfile                      可选 Guardian 容器镜像
```

## 启动 Guardian

根据环境选择一种方式。

### 方式 A：Docker Compose Sidecar

适合 Docker 可以直接访问 NVIDIA GPU 的环境。

检查前提：

```bash
nvidia-smi
docker --version
docker compose version
docker run --rm --gpus all ubuntu nvidia-smi
```

如果最后一条命令能打印 GPU 表格，就可以使用 Docker/Compose 模式。

```bash
git clone https://github.com/CharlieOneDev/vram-guardian-comfyui.git
cd vram-guardian-comfyui
docker compose up -d --build
```

查看状态：

```bash
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
```

如果 Compose 报 `Additional property gpus is not allowed`，说明当前 Compose 对 GPU 字段支持不足，可以改用直接 Docker 或直接 Python 模式。

### 方式 B：直接 Docker

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

如果 Docker 在 NVIDIA runtime 初始化阶段失败，建议使用直接 Python 模式。

### 方式 C：直接 Python

适合 CNB、WSL2、云容器或无法嵌套 GPU Docker 的环境。

```bash
cd vram-guardian-comfyui
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
VRAM_GUARDIAN_FRACTION=0.82 bash scripts/guardian_direct.sh start
```

常用命令：

```bash
bash scripts/guardian_direct.sh status
bash scripts/guardian_direct.sh logs
bash scripts/guardian_direct.sh stop
```

直接模式会生成：

```text
vram_guardian.log
vram_guardian.pid
```

## 安装 ComfyUI 插件

把插件复制到 ComfyUI 的 `custom_nodes` 目录。

Linux：

```bash
cd vram-guardian-comfyui
./scripts/install_plugin.sh /path/to/ComfyUI/custom_nodes
```

Windows PowerShell：

```powershell
cd vram-guardian-comfyui
.\scripts\install_plugin.ps1 -ComfyUICustomNodes "D:\path\to\ComfyUI\custom_nodes"
```

安装或更新插件后，需要重启 ComfyUI。

## 推荐 Scheduler 配置

48GB 共享 GPU 可以从下面配置开始：

```bash
export VRAM_GUARDIAN_HOST=127.0.0.1
export VRAM_GUARDIAN_PORT=8765
export VRAM_GUARDIAN_MAX_RETRY=1

export VRAM_GUARDIAN_SCHEDULER_ENABLE=true
export VRAM_GUARDIAN_BASE_FREE_MB=6144
export VRAM_GUARDIAN_HEAVY_FREE_MB=20480
export VRAM_GUARDIAN_ACTIVE_HYSTERESIS_MB=1024
export VRAM_GUARDIAN_WAIT_TIMEOUT_SEC=120
export VRAM_GUARDIAN_WAIT_POLL_SEC=0.5
export VRAM_GUARDIAN_MONITOR_INTERVAL_SEC=0.5
export VRAM_GUARDIAN_OOM_BUMP_MB=4096

export VRAM_GUARDIAN_NODE_FREE_MAP='{
  "KSampler": 24576,
  "VAEDecode": 16384,
  "VAEDecodeTiled": 12288,
  "UltimateSDUpscale": 24576,
  "SegmentVSRFIStreamRunner": 24576,
  "WanVideoSampler": 24576,
  "LTXVideoSampler": 24576
}'

export VRAM_GUARDIAN_PROFILE_ENABLE=true
export VRAM_GUARDIAN_PROFILE_PATH=./vram_guardian_profile.json
export VRAM_GUARDIAN_PROFILE_MARGIN_MB=2048

python main.py
```

PowerShell 示例：

```powershell
$env:VRAM_GUARDIAN_HOST = "127.0.0.1"
$env:VRAM_GUARDIAN_PORT = "8765"
$env:VRAM_GUARDIAN_SCHEDULER_ENABLE = "true"
$env:VRAM_GUARDIAN_BASE_FREE_MB = "6144"
$env:VRAM_GUARDIAN_HEAVY_FREE_MB = "20480"
$env:VRAM_GUARDIAN_NODE_FREE_MAP = '{"KSampler":24576,"VAEDecode":16384}'
python main.py
```

## Scheduler 工作流程

1. ComfyUI 开始执行 prompt 时，插件开启 base watermark。
2. 普通节点使用 `BASE_FREE_MB` 作为基础空闲显存目标。
3. 如果节点 class 命中 `NODE_FREE_MAP`，插件会把目标 free 提高到对应值。
4. 如果节点 class 在 `HEAVY_NODES` 中但没有 map，则使用 `HEAVY_FREE_MB`。
5. 节点执行前，插件调用 Guardian 的 `ensure_free`，让 Guardian 释放到目标 free。
6. 如果 free 不够，插件会等待，并周期性打印日志。
7. 节点运行中，Guardian 保持该节点目标水位。
8. 节点完成后，高水位 token 关闭，Guardian 回到 base 水位。
9. prompt 结束后，Guardian 回到正常防抢占状态。

## 环境变量

Guardian 进程：

- `VRAM_GUARDIAN_FRACTION`: Guardian 目标占用比例，默认 `0.82`。
- `VRAM_GUARDIAN_MIN_FREE_MB`: 普通回填时保留的空闲显存，默认 `1536`。
- `VRAM_GUARDIAN_CHUNK_MB`: 分配粒度，默认 `256`。
- `VRAM_GUARDIAN_MAX_HOLD_MB`: 绝对占用上限，`0` 表示不限制。
- `VRAM_GUARDIAN_DEVICE`: CUDA 设备，默认 `cuda:0`。
- `VRAM_GUARDIAN_AUTO_REFILL`: 是否启用控制循环，默认 `true`。
- `VRAM_GUARDIAN_WATERMARK_INTERVAL_SEC`: watermark 模式检查间隔，默认 `1`。
- `VRAM_GUARDIAN_WATERMARK_RELEASE_COOLDOWN_SEC`: watermark 释放后多久再补占，默认 `5`。

ComfyUI 插件：

- `VRAM_GUARDIAN_SCHEDULER_ENABLE`: 启用 Scheduler。
- `VRAM_GUARDIAN_BASE_FREE_MB`: 普通阶段目标空闲显存。
- `VRAM_GUARDIAN_HEAVY_FREE_MB`: 未单独配置的重节点目标空闲显存。
- `VRAM_GUARDIAN_NODE_FREE_MAP`: 节点 class 到目标 free 的 JSON 映射。
- `VRAM_GUARDIAN_HEAVY_NODES`: 逗号分隔的重节点 class。
- `VRAM_GUARDIAN_ACTIVE_HYSTERESIS_MB`: 高水位回填缓冲区。
- `VRAM_GUARDIAN_WAIT_TIMEOUT_SEC`: 节点开始前最长等待时间，`0` 表示无限等待。
- `VRAM_GUARDIAN_WAIT_POLL_SEC`: 等待轮询间隔。
- `VRAM_GUARDIAN_WAIT_LOG_INTERVAL_SEC`: 等待日志间隔。
- `VRAM_GUARDIAN_MONITOR_INTERVAL_SEC`: 节点运行期间采样间隔。
- `VRAM_GUARDIAN_PROFILE_ENABLE`: 启用本地 profile。
- `VRAM_GUARDIAN_PROFILE_PATH`: profile JSON 路径，默认 `vram_guardian_profile.json`。
- `VRAM_GUARDIAN_PROFILE_MARGIN_MB`: profile 学习结果额外安全边距。
- `VRAM_GUARDIAN_OOM_BUMP_MB`: OOM 后下次目标提高量。
- `VRAM_GUARDIAN_MAX_RETRY`: OOM 后重试次数，默认 `1`。

## 日志验证

Guardian 日志会显示总显存、空闲显存、Guardian 占用、ComfyUI 占用和其他进程占用：

```text
total=45458MiB free=6144MiB guardian_held=32768MiB target=40960MiB external_calc=6546MiB guardian_proc=33024MiB comfyui=2048MiB other=4498MiB paused=0s
```

Scheduler 日志示例：

```text
[VRAM Scheduler] node=KSampler#12 class=KSampler target_free=24576MiB source=node-map
[VRAM Scheduler] KSampler#12 waiting: free=8192MiB target=24576MiB guardian_held=16384MiB
[VRAM Scheduler] KSampler#12 free reached 24600MiB target=24576MiB; continuing
```

如果 Guardian 已经释放完但 free 仍然不足，说明可能有外部进程占用：

```text
[VRAM Scheduler] KSampler#12 Guardian holds no VRAM but free is still below target; another process may be using the GPU
```

## 调参建议

- 普通节点 OOM：提高 `BASE_FREE_MB`。
- 某个重节点 OOM：提高 `NODE_FREE_MAP` 中该节点的值。
- 防抢占不够强：降低 `BASE_FREE_MB` 或提高 `VRAM_GUARDIAN_FRACTION`。
- 不想长时间等待：降低 `VRAM_GUARDIAN_WAIT_TIMEOUT_SEC`。
- 想让配置越跑越准：启用 `VRAM_GUARDIAN_PROFILE_ENABLE`。
