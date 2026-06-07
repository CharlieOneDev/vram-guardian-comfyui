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

前提：Linux 或 WSL2 Docker，并已安装 NVIDIA Container Toolkit。

```bash
cd /path/to/vram-guardian-comfyui
docker compose up -d --build
```

查看状态：

```bash
docker exec vram-guardian python -m vram_guardian.client status --host 127.0.0.1 --port 8765
```

常用环境变量：

- `VRAM_GUARDIAN_FRACTION`: 目标占用的 GPU 总显存比例。默认：`0.82`。
- `VRAM_GUARDIAN_MIN_FREE_MB`: 填充显存时保留的空闲显存。默认：`1536`。
- `VRAM_GUARDIAN_CHUNK_MB`: 分配粒度。默认：`256`。
- `VRAM_GUARDIAN_MAX_HOLD_MB`: 绝对占用上限，`0` 表示不设置上限。
- `VRAM_GUARDIAN_DEVICE`: CUDA 设备。默认：`cuda:0`。

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
