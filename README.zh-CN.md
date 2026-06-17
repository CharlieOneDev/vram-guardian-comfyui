# VRAM Guardian for ComfyUI

这是一个给 ComfyUI 环境使用的手动显存预占工具。

它启动一个独立的 Guardian 进程，用 CUDA tensor 主动占住显存。默认启动时会把 GPU 总占用推到 `VRAM_GUARDIAN_FRACTION=0.98`，也就是尽量只留下约 2% 空闲显存。之后不会自动释放、自动回填或自动干预 ComfyUI 节点；你需要手动执行命令释放多少 GiB，再手动执行命令重新占回多少 GiB。

## 目录

```text
guardian/vram_guardian/
  server.py   # 显存占用服务
  client.py   # 手动控制客户端

comfyui_plugin/vram_guardian_comfyui/
  __init__.py # ComfyUI 插件入口；默认不再自动 patch 执行流程

scripts/
  guardian_direct.sh # 容器/宿主机内直接启动和控制 Guardian
```

## 推荐用法：放进容器启动脚本

在你的 ComfyUI 容器启动脚本中，先启动 Guardian：

```bash
cd /path/to/vram-guardian-comfyui
bash scripts/guardian_direct.sh start
```

默认行为：

- 启动时预占到 GPU 总占用约 `98%`。
- `VRAM_GUARDIAN_AUTO_REFILL=false`，不会自动补占。
- ComfyUI 插件默认不安装 prompt/node 自动调度补丁。
- `release` 释放后，显存会回到 CUDA driver，任何进程都可能拿到它。

## 手动控制命令

查看状态：

```bash
bash scripts/guardian_direct.sh status
```

释放 8 GiB：

```bash
bash scripts/guardian_direct.sh release 8
```

重新占用 8 GiB：

```bash
bash scripts/guardian_direct.sh reserve 8
```

释放 Guardian 当前持有的全部显存：

```bash
bash scripts/guardian_direct.sh release-all
```

重新补满到目标水位，例如默认 98%：

```bash
bash scripts/guardian_direct.sh fill
```

看日志、停止、重启：

```bash
bash scripts/guardian_direct.sh logs
bash scripts/guardian_direct.sh stop
bash scripts/guardian_direct.sh restart
```

## CNB 中的快捷命令

如果你把插件放在 CNB 的推荐目录：

```bash
/workspace/assets/vram-guardian-comfyui
```

并且 `welcome.sh` 已经启动过 Guardian，它会创建一个 `vram-guardian` 快捷命令。之后可以直接这样手动控制显存：

```bash
vram-guardian status
vram-guardian release 8
vram-guardian reserve 8
vram-guardian fill
vram-guardian release-all
```

命令含义：

- `vram-guardian status`: 查看当前 GPU 显存占用，并区分 Guardian 预占、ComfyUI 和其它进程。
- `vram-guardian release 8`: 从 Guardian 预占显存中释放 `8 GiB`。
- `vram-guardian reserve 8`: 让 Guardian 重新预占最多 `8 GiB`，但不会超过配置的目标水位。
- `vram-guardian fill`: 重新补满到目标水位，默认是 GPU 总占用约 `98%`。
- `vram-guardian release-all`: 释放 Guardian 当前持有的全部显存。

注意：这些命令只控制 Guardian 自己预占的显存。它不能强制 ComfyUI 或其它进程释放显存；释放出来的显存也会变成普通空闲显存，可能被其它 CUDA 进程拿走。

## Python 客户端

如果你直接使用 Python 模块：

```bash
export PYTHONPATH=/path/to/vram-guardian-comfyui/guardian:${PYTHONPATH}

python -m vram_guardian.client status --host 127.0.0.1 --port 8765
python -m vram_guardian.client release --gb 8 --host 127.0.0.1 --port 8765
python -m vram_guardian.client reserve --gb 8 --host 127.0.0.1 --port 8765
python -m vram_guardian.client fill --host 127.0.0.1 --port 8765
```

`reserve`、`occupy`、`hold`、`allocate` 是同义命令。带数量的补占命令最多补占指定数量，并且不会超过配置的目标水位。`fill` 不带数量时补到目标水位。

## Docker Compose

```bash
docker compose up -d --build
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
docker exec vram-guardian python -m vram_guardian.client release --gb 8 --host 127.0.0.1 --port 8765
docker exec vram-guardian python -m vram_guardian.client reserve --gb 8 --host 127.0.0.1 --port 8765
```

Compose 默认值已经改成：

```yaml
VRAM_GUARDIAN_FRACTION: "0.98"
VRAM_GUARDIAN_MIN_FREE_MB: "0"
VRAM_GUARDIAN_AUTO_REFILL: "false"
```

## 常用环境变量

- `VRAM_GUARDIAN_FRACTION`: 目标 GPU 总占用比例，默认 `0.98`，最大会限制到 `0.98`。
- `VRAM_GUARDIAN_MIN_FREE_MB`: 预占和手动补占时至少保留的空闲显存，默认 `0`。
- `VRAM_GUARDIAN_CHUNK_MB`: 分配粒度，默认 `256`。
- `VRAM_GUARDIAN_MAX_HOLD_MB`: Guardian 自身持有显存上限，`0` 表示不限制。
- `VRAM_GUARDIAN_DEVICE`: CUDA 设备，默认 `cuda:0`。
- `VRAM_GUARDIAN_HOST`: 服务监听地址，默认脚本中为 `0.0.0.0`。
- `VRAM_GUARDIAN_PORT`: 服务端口，默认 `8765`。
- `VRAM_GUARDIAN_AUTO_REFILL`: 是否启用旧的自动回填循环，默认 `false`。
- `VRAM_GUARDIAN_COMFYUI_AUTOMATION`: 是否让 ComfyUI 插件安装旧的自动调度 patch，默认 `false`。

## ComfyUI 插件行为

ComfyUI 插件默认只加载并记录 Guardian 状态，不再自动：

- prompt 开始时设置 watermark；
- 节点开始前释放显存；
- OOM 后释放和重试；
- 节点成功后自动 reclaim。

如果你确实想临时使用旧的自动逻辑，可以显式设置：

```bash
export VRAM_GUARDIAN_COMFYUI_AUTOMATION=true
export VRAM_GUARDIAN_SCHEDULER_PRESET=heavy-video
export VRAM_GUARDIAN_RECLAIM_ON_SUCCESS=true
```

手动模式下通常不需要这些变量。

## 注意

Guardian 不是私有显存池。你手动释放的显存会变成普通空闲显存，其它 CUDA 进程也可能抢走；Guardian 只能重新占用当前还能拿到的部分。
