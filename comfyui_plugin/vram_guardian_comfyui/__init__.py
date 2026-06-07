import asyncio
import inspect
import json
import logging
import os
import socket
import time
from typing import Any

import torch


NODE_CLASS_MAPPINGS: dict[str, type] = {}
NODE_DISPLAY_NAME_MAPPINGS: dict[str, str] = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

LOG = logging.getLogger("vram_guardian_comfyui")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or value == "" else int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None or value == "" else float(value)


ENABLED = _env_bool("VRAM_GUARDIAN_ENABLED", True)
HOST = os.getenv("VRAM_GUARDIAN_HOST", "127.0.0.1")
PORT = _env_int("VRAM_GUARDIAN_PORT", 8765)
TIMEOUT = _env_float("VRAM_GUARDIAN_TIMEOUT_SEC", 2.0)
MAX_RETRY = _env_int("VRAM_GUARDIAN_MAX_RETRY", 1)
RELEASE_MB = _env_int("VRAM_GUARDIAN_RELEASE_MB", 0)
RELEASE_REFILL_PAUSE = _env_float("VRAM_GUARDIAN_RELEASE_REFILL_PAUSE_SEC", 3600.0)
RELEASE_BEFORE_NODE = _env_bool("VRAM_GUARDIAN_RELEASE_BEFORE_NODE", False)
RETRY_SLEEP = _env_float("VRAM_GUARDIAN_RETRY_SLEEP_SEC", 0.5)
RECLAIM_ON_SUCCESS = _env_bool("VRAM_GUARDIAN_RECLAIM_ON_SUCCESS", True)
RECLAIM_DELAY = _env_float("VRAM_GUARDIAN_RECLAIM_DELAY_SEC", 0.0)
ACTIVE_FREE_MB = _env_int("VRAM_GUARDIAN_ACTIVE_FREE_MB", 0)
ACTIVE_HYSTERESIS_MB = _env_int("VRAM_GUARDIAN_ACTIVE_HYSTERESIS_MB", 2048)
ACTIVE_RECLAIM_ON_EXIT = _env_bool("VRAM_GUARDIAN_ACTIVE_RECLAIM_ON_EXIT", True)
HEAVY_NODES = {name.strip() for name in os.getenv("VRAM_GUARDIAN_HEAVY_NODES", "").split(",") if name.strip()}


def _guardian_request(cmd: str, **fields: Any) -> dict[str, Any] | None:
    payload = {"cmd": cmd, **fields}
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with socket.create_connection((HOST, PORT), timeout=TIMEOUT) as sock:
            sock.settimeout(TIMEOUT)
            sock.sendall(data)
            raw = sock.recv(65536).split(b"\n", 1)[0]
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        LOG.warning("VRAM Guardian request %s failed: %s", cmd, exc)
        return None


def _set_watermark(mode: bool, free_mb: int | None = None, hysteresis_mb: int | None = None) -> dict[str, Any] | None:
    fields: dict[str, Any] = {"mode": mode}
    if free_mb is not None:
        fields["free_mb"] = free_mb
    if hysteresis_mb is not None:
        fields["hysteresis_mb"] = hysteresis_mb
    response = _guardian_request("set_watermark", **fields)
    LOG.info("VRAM Guardian watermark response: %s", response)
    return response


def _release_guardian() -> None:
    fields: dict[str, Any] = {"pause_refill_sec": RELEASE_REFILL_PAUSE}
    if RELEASE_MB > 0:
        fields["mb"] = RELEASE_MB
    response = _guardian_request("release", **fields)
    LOG.info("VRAM Guardian release response: %s", response)


def _reclaim_guardian() -> None:
    if RECLAIM_DELAY > 0:
        time.sleep(RECLAIM_DELAY)
    response = _guardian_request("reclaim")
    LOG.debug("VRAM Guardian reclaim response: %s", response)


def _local_cuda_cleanup() -> None:
    if not torch.cuda.is_available():
        return
    try:
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()
    except Exception:
        LOG.debug("local CUDA cleanup failed", exc_info=True)


def _is_oom(exc: BaseException) -> bool:
    if isinstance(exc, torch.cuda.OutOfMemoryError):
        return True
    if isinstance(exc, RuntimeError):
        text = str(exc).lower()
        return "out of memory" in text and ("cuda" in text or "gpu" in text or "cublas" in text)
    return False


def _node_label(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    unique_id = kwargs.get("unique_id")
    obj = kwargs.get("obj")
    if len(args) > 1 and unique_id is None:
        unique_id = args[1]
    if len(args) > 2 and obj is None:
        obj = args[2]
    node_name = type(obj).__name__ if obj is not None else "unknown"
    return f"{node_name}#{unique_id}" if unique_id is not None else node_name


def _node_obj(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if "obj" in kwargs:
        return kwargs["obj"]
    if len(args) > 2:
        return args[2]
    return None


def _is_active_watermark_node(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    if ACTIVE_FREE_MB <= 0:
        return False
    if not HEAVY_NODES:
        return True
    obj = _node_obj(args, kwargs)
    return obj is not None and type(obj).__name__ in HEAVY_NODES


async def _async_sleep() -> None:
    if RETRY_SLEEP > 0:
        await asyncio.sleep(RETRY_SLEEP)


def _install_get_output_data_patch() -> None:
    import execution

    original = execution.get_output_data
    if getattr(original, "_vram_guardian_patched", False):
        return

    if inspect.iscoroutinefunction(original):

        async def patched_get_output_data(*args: Any, **kwargs: Any) -> Any:
            label = _node_label(args, kwargs)
            active_watermark = _is_active_watermark_node(args, kwargs)
            for attempt in range(MAX_RETRY + 1):
                watermark_enabled = False
                succeeded = False
                try:
                    if active_watermark:
                        LOG.info(
                            "enabling Guardian active watermark before node %s: free=%sMB hysteresis=%sMB",
                            label,
                            ACTIVE_FREE_MB,
                            ACTIVE_HYSTERESIS_MB,
                        )
                        _set_watermark(True, ACTIVE_FREE_MB, ACTIVE_HYSTERESIS_MB)
                        watermark_enabled = True
                        _local_cuda_cleanup()
                        await _async_sleep()
                    if RELEASE_BEFORE_NODE and not active_watermark and attempt == 0:
                        LOG.info("releasing Guardian VRAM before node %s", label)
                        _release_guardian()
                        _local_cuda_cleanup()
                        await _async_sleep()
                    result = await original(*args, **kwargs)
                    succeeded = True
                    if RECLAIM_ON_SUCCESS and not active_watermark:
                        _reclaim_guardian()
                    return result
                except Exception as exc:
                    if not _is_oom(exc) or attempt >= MAX_RETRY:
                        raise
                    LOG.warning(
                        "CUDA OOM in %s; releasing Guardian VRAM and retrying (%s/%s)",
                        label,
                        attempt + 1,
                        MAX_RETRY,
                    )
                    _release_guardian()
                    _local_cuda_cleanup()
                    await _async_sleep()
                finally:
                    if watermark_enabled:
                        _set_watermark(False)
                        if succeeded and ACTIVE_RECLAIM_ON_EXIT and RECLAIM_ON_SUCCESS:
                            _reclaim_guardian()

        patched_get_output_data._vram_guardian_patched = True  # type: ignore[attr-defined]
        patched_get_output_data._vram_guardian_original = original  # type: ignore[attr-defined]
        execution.get_output_data = patched_get_output_data
        LOG.info("installed async get_output_data VRAM Guardian patch")
        return

    def patched_get_output_data(*args: Any, **kwargs: Any) -> Any:
        label = _node_label(args, kwargs)
        active_watermark = _is_active_watermark_node(args, kwargs)
        for attempt in range(MAX_RETRY + 1):
            watermark_enabled = False
            succeeded = False
            try:
                if active_watermark:
                    LOG.info(
                        "enabling Guardian active watermark before node %s: free=%sMB hysteresis=%sMB",
                        label,
                        ACTIVE_FREE_MB,
                        ACTIVE_HYSTERESIS_MB,
                    )
                    _set_watermark(True, ACTIVE_FREE_MB, ACTIVE_HYSTERESIS_MB)
                    watermark_enabled = True
                    _local_cuda_cleanup()
                    if RETRY_SLEEP > 0:
                        time.sleep(RETRY_SLEEP)
                if RELEASE_BEFORE_NODE and not active_watermark and attempt == 0:
                    LOG.info("releasing Guardian VRAM before node %s", label)
                    _release_guardian()
                    _local_cuda_cleanup()
                    if RETRY_SLEEP > 0:
                        time.sleep(RETRY_SLEEP)
                result = original(*args, **kwargs)
                succeeded = True
                if RECLAIM_ON_SUCCESS and not active_watermark:
                    _reclaim_guardian()
                return result
            except Exception as exc:
                if not _is_oom(exc) or attempt >= MAX_RETRY:
                    raise
                LOG.warning(
                    "CUDA OOM in %s; releasing Guardian VRAM and retrying (%s/%s)",
                    label,
                    attempt + 1,
                    MAX_RETRY,
                )
                _release_guardian()
                _local_cuda_cleanup()
                if RETRY_SLEEP > 0:
                    time.sleep(RETRY_SLEEP)
            finally:
                if watermark_enabled:
                    _set_watermark(False)
                    if succeeded and ACTIVE_RECLAIM_ON_EXIT and RECLAIM_ON_SUCCESS:
                        _reclaim_guardian()

    patched_get_output_data._vram_guardian_patched = True  # type: ignore[attr-defined]
    patched_get_output_data._vram_guardian_original = original  # type: ignore[attr-defined]
    execution.get_output_data = patched_get_output_data
    LOG.info("installed sync get_output_data VRAM Guardian patch")


def _install_async_result_patch() -> None:
    import execution

    original = getattr(execution, "resolve_map_node_over_list_results", None)
    if original is None or getattr(original, "_vram_guardian_patched", False):
        return
    if not inspect.iscoroutinefunction(original):
        return

    async def patched_resolve_map_node_over_list_results(*args: Any, **kwargs: Any) -> Any:
        try:
            return await original(*args, **kwargs)
        except Exception as exc:
            if _is_oom(exc):
                LOG.warning("CUDA OOM surfaced from async node task; releasing Guardian VRAM")
                _release_guardian()
                _local_cuda_cleanup()
            raise

    patched_resolve_map_node_over_list_results._vram_guardian_patched = True  # type: ignore[attr-defined]
    patched_resolve_map_node_over_list_results._vram_guardian_original = original  # type: ignore[attr-defined]
    execution.resolve_map_node_over_list_results = patched_resolve_map_node_over_list_results
    LOG.info("installed async task result VRAM Guardian patch")


def _install() -> None:
    if not ENABLED:
        LOG.info("VRAM Guardian plugin disabled by VRAM_GUARDIAN_ENABLED")
        return
    try:
        _install_get_output_data_patch()
        _install_async_result_patch()
        status = _guardian_request("status")
        LOG.info("VRAM Guardian plugin loaded; guardian status: %s", status)
    except Exception:
        LOG.exception("failed to install VRAM Guardian plugin")


_install()
