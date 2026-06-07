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
- OOM 后全释放、清理 CUDA cache、等待更高的 retry 空闲显存目标、重试一次，并可提高该节点下次预算。
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

## 容错模型

VRAM Guardian 分三层处理故障：

1. 预防：估算节点突发显存目标，在节点开始前释放，并在重节点运行期间保持 no-refill lease。
2. 同进程 OOM retry：如果 ComfyUI 捕获到 CUDA OOM，Guardian 会全释放，ComfyUI 清理未使用的 CUDA cache，插件等待更高的 retry 目标，然后默认重试该节点一次。
3. 进程重启：如果 ComfyUI 进程本身崩溃，插件无法从任意 custom node 的中途恢复。外部 supervisor 可以重启 ComfyUI 并重新排队 prompt，但真正的断点续跑需要工作流主动写入可持久化中间文件，例如 latent、图片或视频分段。

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

ComfyUI 插件现在默认使用 `heavy-video` scheduler preset。这个 preset 面向重视频工作流：载入视频帧和参考图、pose/control 预处理、Wan/LTX 类视频模型生成、VAE decode、插帧、放大或 VSR。

Guardian 已经运行时，ComfyUI 侧通常只需要：

```bash
export VRAM_GUARDIAN_HOST=127.0.0.1
export VRAM_GUARDIAN_PORT=8765
export VRAM_GUARDIAN_MAX_RETRY=1

python main.py
```

默认 `heavy-video` 行为：

- Scheduler 默认启用。
- 基础 workflow 空闲显存目标会自动计算，并刻意保持低暴露：`min(total_vram * 0.14, 6144 MiB)`。
- 重节点 fallback 空闲显存目标自动计算：`min(total_vram * 0.72, 32768 MiB)`。
- Estimator 默认启用。它会读取节点已解析输入，例如宽、高、帧数、tensor shape、tiling、scale、模型名、精度和 offload 标记，然后计算 `target_free = estimated_peak_total - current_comfyui_used + margin`。
- 重节点 lease 默认是 `no-refill`：节点运行期间 Guardian 可以继续释放显存，但不会主动把多余 free 显存占回去，直到该节点结束。
- 本地 profiling 默认启用，写入 `vram_guardian_profile.json`。
- 重节点通过宽泛 class-name 模糊匹配识别，例如 `sampler`、`wan`、`ltx`、`bernini`、`vsr`、`upscale`、`interpol`、`decode`、`vae`、`pose`、`model`。

在 48 GiB/L40 级别 GPU 上，大致相当于：

```text
base free target:  约 6 GiB
heavy fallback target: 约 32 GiB
```

这样在 workflow 空转、等待或执行轻节点时，暴露在 free 状态的显存会尽量少。重节点即将开始时，如果 estimator 能拿到足够参数，Guardian 只释放估算出的 burst window；如果参数不足，再退回 heavy target。

仍然可以手动覆盖：

```bash
export VRAM_GUARDIAN_BASE_FREE_MB=32768
export VRAM_GUARDIAN_HEAVY_FREE_MB=36864
export VRAM_GUARDIAN_HEAVY_PATTERNS=sampler,wan,ltx,bernini,vsr,upscale,interpol,decode,vae,pose,model
python main.py
```

如果想恢复旧逻辑，也就是只有显式设置变量才启用 scheduler target，可以设置：

```bash
export VRAM_GUARDIAN_SCHEDULER_PRESET=manual
```

PowerShell 示例：

```powershell
$env:VRAM_GUARDIAN_HOST = "127.0.0.1"
$env:VRAM_GUARDIAN_PORT = "8765"
python main.py
```

## Scheduler 工作流程

1. ComfyUI 开始执行 prompt 时，插件开启 base watermark。
2. 普通节点使用 `BASE_FREE_MB` 作为基础空闲显存目标。
3. 如果节点 class 命中 `NODE_FREE_MAP`，插件会把目标 free 提高到对应值。
4. 如果 estimator 可以根据节点输入估算峰值，则优先使用估算出的 target free。
5. 如果节点 class 在 `HEAVY_NODES` 中，或命中 `HEAVY_PATTERNS`，但参数不足以估算，则使用 `HEAVY_FREE_MB`。
6. 节点执行前，插件调用 Guardian 的 `ensure_free`，让 Guardian 释放到目标 free。
7. 如果 free 不够，插件会等待，并周期性打印日志。
8. 重节点运行中默认 no-refill，Guardian 只释放不回填。
9. 节点完成后，高水位 token 关闭，Guardian 回到 base 水位。
10. prompt 结束后，Guardian 回到正常防抢占状态。

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

- `VRAM_GUARDIAN_SCHEDULER_PRESET`: scheduler preset，默认 `heavy-video`。设置为 `manual` 或 `off` 可关闭自动 heavy-video 默认值。
- `VRAM_GUARDIAN_SCHEDULER_ENABLE`: 启用 Scheduler，`heavy-video` 下默认启用。
- `VRAM_GUARDIAN_BASE_FREE_MB`: 显式基础空闲显存目标。未设置时，`heavy-video` 使用 `min(total_vram * 0.14, 6144 MiB)`。
- `VRAM_GUARDIAN_HEAVY_FREE_MB`: 显式重节点空闲显存目标。未设置时，`heavy-video` 使用 `min(total_vram * 0.72, 32768 MiB)`。
- `VRAM_GUARDIAN_AUTO_BASE_FREE_FRACTION`: 自动基础目标比例，默认 `0.14`。
- `VRAM_GUARDIAN_AUTO_HEAVY_FREE_FRACTION`: 自动重节点目标比例，默认 `0.72`。
- `VRAM_GUARDIAN_AUTO_BASE_FREE_CAP_MB`: 自动基础目标上限，默认 `6144`。
- `VRAM_GUARDIAN_AUTO_HEAVY_FREE_CAP_MB`: 自动重节点目标上限，默认 `32768`。
- `VRAM_GUARDIAN_ESTIMATOR_ENABLE`: 根据节点已解析输入估算 target free，`heavy-video` 下默认启用。
- `VRAM_GUARDIAN_ESTIMATOR_MARGIN_MB`: estimator 额外安全边距，默认 `2048`。
- `VRAM_GUARDIAN_ESTIMATOR_MAX_FREE_MB`: estimator target free 上限，`0` 表示使用 GPU 总显存减保留值。
- `VRAM_GUARDIAN_HEAVY_REFILL_MODE`: 重节点 lease 回填策略，默认 `no-refill`。设置为 `refill` 可允许重节点期间回填。
- `VRAM_GUARDIAN_NODE_FREE_MAP`: 节点 class 到目标 free 的 JSON 映射。
- `VRAM_GUARDIAN_HEAVY_NODES`: 逗号分隔的重节点 class。
- `VRAM_GUARDIAN_HEAVY_PATTERNS`: 逗号分隔的小写 class-name 片段，匹配到就按重节点处理。`heavy-video` 下默认是视频工作流常见关键词。
- `VRAM_GUARDIAN_ACTIVE_HYSTERESIS_MB`: 高水位回填缓冲区。
- `VRAM_GUARDIAN_WAIT_TIMEOUT_SEC`: 节点开始前最长等待时间，`0` 表示无限等待。
- `VRAM_GUARDIAN_WAIT_POLL_SEC`: 等待轮询间隔。
- `VRAM_GUARDIAN_WAIT_LOG_INTERVAL_SEC`: 等待日志间隔。
- `VRAM_GUARDIAN_MONITOR_INTERVAL_SEC`: 节点运行期间采样间隔。
- `VRAM_GUARDIAN_PROFILE_ENABLE`: 启用本地 profile，`heavy-video` 下默认启用。
- `VRAM_GUARDIAN_PROFILE_PATH`: profile JSON 路径，默认 `vram_guardian_profile.json`。
- `VRAM_GUARDIAN_PROFILE_MARGIN_MB`: profile 学习结果额外安全边距。
- `VRAM_GUARDIAN_OOM_BUMP_MB`: OOM 后下次目标提高量。
- `VRAM_GUARDIAN_OOM_RETRY_FREE_MB`: OOM retry 前显式等待的空闲显存目标。默认 `0` 表示由插件根据原目标、heavy 目标和 OOM bump 自动计算。
- `VRAM_GUARDIAN_OOM_RETRY_RESERVE_MB`: 自动计算 OOM retry 目标时预留的安全边距，默认等于 `VRAM_GUARDIAN_AUTO_FREE_RESERVE_MB`。
- `VRAM_GUARDIAN_MAX_RETRY`: OOM 后重试次数，默认 `1`。

## 日志验证

Guardian 日志会显示总显存、空闲显存、Guardian 占用、ComfyUI 占用和其他进程占用：

```text
total=45458MiB free=6144MiB guardian_held=33200MiB target=37275MiB external_calc=6114MiB guardian_proc=33200MiB comfyui=4096MiB other=2018MiB paused=0s
```

Scheduler 日志示例：

```text
[VRAM Estimator] class=WanVideoSampler peak_total=43008MiB current_comfyui=12288MiB target_free=32768MiB source=estimate-video-sampler details={'width': 576, 'height': 1024, 'frames': 161, 'active_frames': 81}
[VRAM Scheduler] node=WanVideoSampler#12 class=WanVideoSampler target_free=32768MiB source=estimate-video-sampler allow_refill=False
[VRAM Scheduler] WanVideoSampler#12 waiting: free=6144MiB target=32768MiB guardian_held=26624MiB
[VRAM Scheduler] WanVideoSampler#12 free reached 32800MiB target=32768MiB; continuing
```

如果 Guardian 已经释放完但 free 仍然不足，说明可能有外部进程占用：

```text
[VRAM Scheduler] WanVideoSampler#12 Guardian holds no VRAM but free is still below target; another process may be using the GPU
```

## 调参建议

- 重视频工作流 OOM：提高 `ESTIMATOR_MARGIN_MB`、`HEAVY_FREE_MB` 或 `ESTIMATOR_MAX_FREE_MB`。
- 日志显示某个特定 class 反复 OOM：再给 `NODE_FREE_MAP` 加单独目标。
- 防抢占不够强：降低 `BASE_FREE_MB`、`HEAVY_FREE_MB` 或自动上限，或者提高 `VRAM_GUARDIAN_FRACTION`。
- 不想长时间等待：降低 `VRAM_GUARDIAN_WAIT_TIMEOUT_SEC`。
- 想让配置越跑越准：启用 `VRAM_GUARDIAN_PROFILE_ENABLE`。
