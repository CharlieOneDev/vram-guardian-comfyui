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
ACTIVE_SCOPE = os.getenv("VRAM_GUARDIAN_ACTIVE_SCOPE", "prompt").strip().lower()
HEAVY_NODES = {name.strip() for name in os.getenv("VRAM_GUARDIAN_HEAVY_NODES", "").split(",") if name.strip()}
PROMPT_SCOPES = {"prompt", "workflow", "comfyui"}
NODE_SCOPES = {"node", "nodes"}
_PROMPT_WATERMARK_TOKEN: str | None = None
_PROMPT_WATERMARK_LABEL: str | None = None
_PROMPT_SCOPE_PATCHED = False


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


def _set_watermark(
    mode: bool,
    free_mb: int | None = None,
    hysteresis_mb: int | None = None,
    token: str | None = None,
) -> dict[str, Any] | None:
    fields: dict[str, Any] = {"mode": mode}
    if free_mb is not None:
        fields["free_mb"] = free_mb
    if hysteresis_mb is not None:
        fields["hysteresis_mb"] = hysteresis_mb
    if token is not None:
        fields["token"] = token
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


def _node_class_name(obj: Any) -> str:
    if obj is None:
        return "unknown"
    if inspect.isclass(obj):
        return obj.__name__
    return type(obj).__name__


def _looks_like_node(obj: Any) -> bool:
    if obj is None:
        return False
    if inspect.isclass(obj):
        return hasattr(obj, "FUNCTION")
    return hasattr(obj, "FUNCTION") or hasattr(type(obj), "FUNCTION")


def _node_obj(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if "obj" in kwargs:
        return kwargs["obj"]
    if len(args) > 2 and _looks_like_node(args[2]):
        return args[2]
    if args and _looks_like_node(args[0]):
        return args[0]
    return None


def _node_unique_id(args: tuple[Any, ...], kwargs: dict[str, Any], obj: Any) -> Any:
    unique_id = kwargs.get("unique_id")
    if unique_id is None and len(args) > 2 and obj is args[2] and len(args) > 1:
        unique_id = args[1]
    return unique_id


def _node_label(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    obj = _node_obj(args, kwargs)
    unique_id = _node_unique_id(args, kwargs, obj)
    node_name = _node_class_name(obj)
    return f"{node_name}#{unique_id}" if unique_id is not None else node_name


def _prompt_label(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    prompt_id = kwargs.get("prompt_id")
    if prompt_id is None and len(args) > 1:
        prompt_id = args[1]
    return f"prompt#{prompt_id}" if prompt_id is not None else "prompt"


def _is_active_watermark_node(args: tuple[Any, ...], kwargs: dict[str, Any]) -> bool:
    if ACTIVE_FREE_MB <= 0 or ACTIVE_SCOPE not in NODE_SCOPES:
        return False
    if not HEAVY_NODES:
        return True
    obj = _node_obj(args, kwargs)
    return obj is not None and _node_class_name(obj) in HEAVY_NODES


def _is_active_watermark_prompt() -> bool:
    return _PROMPT_SCOPE_PATCHED and ACTIVE_FREE_MB > 0 and ACTIVE_SCOPE in PROMPT_SCOPES


def _watermark_token(label: str) -> str:
    return f"{os.getpid()}:{label}:{time.monotonic_ns()}"


def _begin_watermark(label: str) -> str:
    token = _watermark_token(label)
    LOG.info(
        "enabling Guardian active watermark for %s: free=%sMB hysteresis=%sMB token=%s",
        label,
        ACTIVE_FREE_MB,
        ACTIVE_HYSTERESIS_MB,
        token,
    )
    _set_watermark(True, ACTIVE_FREE_MB, ACTIVE_HYSTERESIS_MB, token=token)
    _local_cuda_cleanup()
    return token


def _pending_tasks_from_result(result: Any) -> list[asyncio.Task[Any]]:
    if not isinstance(result, tuple) or len(result) < 4 or not result[3]:
        return []
    values = result[0]
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, asyncio.Task) and not value.done()]


def _end_watermark(token: str | None, label: str) -> None:
    if token is None:
        return
    LOG.info("disabling Guardian active watermark for %s", label)
    _set_watermark(False, token=token)


def _suspend_prompt_watermark(reason: str) -> None:
    global _PROMPT_WATERMARK_LABEL, _PROMPT_WATERMARK_TOKEN

    if _PROMPT_WATERMARK_TOKEN is None:
        return
    label = _PROMPT_WATERMARK_LABEL or "prompt"
    LOG.warning("suspending Guardian prompt watermark for %s: %s", label, reason)
    _end_watermark(_PROMPT_WATERMARK_TOKEN, label)
    _PROMPT_WATERMARK_TOKEN = None
    _PROMPT_WATERMARK_LABEL = None


def _reclaim_after_active_scope(label: str) -> None:
    if ACTIVE_RECLAIM_ON_EXIT and RECLAIM_ON_SUCCESS:
        LOG.info("reclaiming Guardian VRAM after active watermark scope %s", label)
        _reclaim_guardian()


def _watch_pending_tasks(tasks: list[asyncio.Task[Any]], token: str, label: str) -> None:
    async def wait_and_close() -> None:
        failed = True
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failed = any(isinstance(result, BaseException) for result in results)
            if failed:
                LOG.warning("pending tasks for %s ended with an error; Guardian will not reclaim immediately", label)
        finally:
            _end_watermark(token, label)
            if not failed:
                _reclaim_after_active_scope(label)

    asyncio.create_task(wait_and_close())


async def _async_sleep() -> None:
    if RETRY_SLEEP > 0:
        await asyncio.sleep(RETRY_SLEEP)


def _install_prompt_patch() -> None:
    global _PROMPT_SCOPE_PATCHED

    import execution

    executor_cls = getattr(execution, "PromptExecutor", None)
    if executor_cls is None:
        LOG.warning("PromptExecutor not found; prompt-scope VRAM Guardian patch is unavailable")
        return

    original_async = getattr(executor_cls, "execute_async", None)
    if original_async is not None and getattr(original_async, "_vram_guardian_prompt_patched", False):
        _PROMPT_SCOPE_PATCHED = True
        return
    if original_async is not None:

        async def patched_execute_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            global _PROMPT_WATERMARK_LABEL, _PROMPT_WATERMARK_TOKEN

            label = _prompt_label(args, kwargs)
            token: str | None = None
            if _is_active_watermark_prompt():
                token = _begin_watermark(label)
                _PROMPT_WATERMARK_TOKEN = token
                _PROMPT_WATERMARK_LABEL = label
                await _async_sleep()
            try:
                return await original_async(self, *args, **kwargs)
            finally:
                if token is not None and _PROMPT_WATERMARK_TOKEN == token:
                    _end_watermark(token, label)
                    _PROMPT_WATERMARK_TOKEN = None
                    _PROMPT_WATERMARK_LABEL = None
                    _reclaim_after_active_scope(label)
                elif token is not None:
                    _reclaim_after_active_scope(label)

        patched_execute_async._vram_guardian_prompt_patched = True  # type: ignore[attr-defined]
        patched_execute_async._vram_guardian_original = original_async  # type: ignore[attr-defined]
        executor_cls.execute_async = patched_execute_async
        _PROMPT_SCOPE_PATCHED = True
        LOG.info("installed prompt-scope async VRAM Guardian patch")
        return

    original_sync = getattr(executor_cls, "execute", None)
    if original_sync is None:
        return
    if getattr(original_sync, "_vram_guardian_prompt_patched", False):
        _PROMPT_SCOPE_PATCHED = True
        return

    def patched_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
        global _PROMPT_WATERMARK_LABEL, _PROMPT_WATERMARK_TOKEN

        label = _prompt_label(args, kwargs)
        token: str | None = None
        if _is_active_watermark_prompt():
            token = _begin_watermark(label)
            _PROMPT_WATERMARK_TOKEN = token
            _PROMPT_WATERMARK_LABEL = label
            if RETRY_SLEEP > 0:
                time.sleep(RETRY_SLEEP)
        try:
            return original_sync(self, *args, **kwargs)
        finally:
            if token is not None and _PROMPT_WATERMARK_TOKEN == token:
                _end_watermark(token, label)
                _PROMPT_WATERMARK_TOKEN = None
                _PROMPT_WATERMARK_LABEL = None
                _reclaim_after_active_scope(label)
            elif token is not None:
                _reclaim_after_active_scope(label)

    patched_execute._vram_guardian_prompt_patched = True  # type: ignore[attr-defined]
    patched_execute._vram_guardian_original = original_sync  # type: ignore[attr-defined]
    executor_cls.execute = patched_execute
    _PROMPT_SCOPE_PATCHED = True
    LOG.info("installed prompt-scope sync VRAM Guardian patch")


def _install_get_output_data_patch() -> None:
    import execution

    original = execution.get_output_data
    if getattr(original, "_vram_guardian_patched", False):
        return

    if inspect.iscoroutinefunction(original):

        async def patched_get_output_data(*args: Any, **kwargs: Any) -> Any:
            label = _node_label(args, kwargs)
            active_watermark = _is_active_watermark_node(args, kwargs)
            if active_watermark or ACTIVE_SCOPE in NODE_SCOPES:
                LOG.info("VRAM Guardian node %s active_watermark=%s", label, active_watermark)
            else:
                LOG.debug("VRAM Guardian node %s active_watermark=%s", label, active_watermark)
            full_release_retry = False
            for attempt in range(MAX_RETRY + 1):
                token: str | None = None
                succeeded = False
                try:
                    if active_watermark and not full_release_retry:
                        token = _watermark_token(label)
                        LOG.info(
                            "enabling Guardian active watermark before node %s: free=%sMB hysteresis=%sMB token=%s",
                            label,
                            ACTIVE_FREE_MB,
                            ACTIVE_HYSTERESIS_MB,
                            token,
                        )
                        _set_watermark(True, ACTIVE_FREE_MB, ACTIVE_HYSTERESIS_MB, token=token)
                        _local_cuda_cleanup()
                        await _async_sleep()
                    elif active_watermark and full_release_retry:
                        LOG.info("running full-release retry for node %s without watermark refill", label)
                    if RELEASE_BEFORE_NODE and not active_watermark and not _is_active_watermark_prompt() and attempt == 0:
                        LOG.info("releasing Guardian VRAM before node %s", label)
                        _release_guardian()
                        _local_cuda_cleanup()
                        await _async_sleep()
                    result = await original(*args, **kwargs)
                    succeeded = True
                    if active_watermark and token is not None:
                        pending_tasks = _pending_tasks_from_result(result)
                        if pending_tasks:
                            LOG.info(
                                "keeping Guardian active watermark for node %s until %s pending task(s) finish",
                                label,
                                len(pending_tasks),
                            )
                            _watch_pending_tasks(pending_tasks, token, label)
                            token = None
                    elif active_watermark and full_release_retry:
                        _reclaim_after_active_scope(label)
                    elif RECLAIM_ON_SUCCESS and not _is_active_watermark_prompt():
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
                    if token is not None:
                        _end_watermark(token, label)
                        token = None
                    if _is_active_watermark_prompt():
                        _suspend_prompt_watermark(f"CUDA OOM in {label}")
                    _release_guardian()
                    full_release_retry = True
                    _local_cuda_cleanup()
                    await _async_sleep()
                finally:
                    if token is not None:
                        _end_watermark(token, label)
                        if succeeded:
                            _reclaim_after_active_scope(label)

        patched_get_output_data._vram_guardian_patched = True  # type: ignore[attr-defined]
        patched_get_output_data._vram_guardian_original = original  # type: ignore[attr-defined]
        execution.get_output_data = patched_get_output_data
        LOG.info("installed async get_output_data VRAM Guardian patch")
        return

    def patched_get_output_data(*args: Any, **kwargs: Any) -> Any:
        label = _node_label(args, kwargs)
        active_watermark = _is_active_watermark_node(args, kwargs)
        if active_watermark or ACTIVE_SCOPE in NODE_SCOPES:
            LOG.info("VRAM Guardian node %s active_watermark=%s", label, active_watermark)
        else:
            LOG.debug("VRAM Guardian node %s active_watermark=%s", label, active_watermark)
        full_release_retry = False
        for attempt in range(MAX_RETRY + 1):
            token: str | None = None
            succeeded = False
            try:
                if active_watermark and not full_release_retry:
                    token = _watermark_token(label)
                    LOG.info(
                        "enabling Guardian active watermark before node %s: free=%sMB hysteresis=%sMB token=%s",
                        label,
                        ACTIVE_FREE_MB,
                        ACTIVE_HYSTERESIS_MB,
                        token,
                    )
                    _set_watermark(True, ACTIVE_FREE_MB, ACTIVE_HYSTERESIS_MB, token=token)
                    _local_cuda_cleanup()
                    if RETRY_SLEEP > 0:
                        time.sleep(RETRY_SLEEP)
                elif active_watermark and full_release_retry:
                    LOG.info("running full-release retry for node %s without watermark refill", label)
                if RELEASE_BEFORE_NODE and not active_watermark and not _is_active_watermark_prompt() and attempt == 0:
                    LOG.info("releasing Guardian VRAM before node %s", label)
                    _release_guardian()
                    _local_cuda_cleanup()
                    if RETRY_SLEEP > 0:
                        time.sleep(RETRY_SLEEP)
                result = original(*args, **kwargs)
                succeeded = True
                if active_watermark and full_release_retry:
                    _reclaim_after_active_scope(label)
                elif RECLAIM_ON_SUCCESS and not active_watermark and not _is_active_watermark_prompt():
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
                if token is not None:
                    _end_watermark(token, label)
                    token = None
                if _is_active_watermark_prompt():
                    _suspend_prompt_watermark(f"CUDA OOM in {label}")
                _release_guardian()
                full_release_retry = True
                _local_cuda_cleanup()
                if RETRY_SLEEP > 0:
                    time.sleep(RETRY_SLEEP)
            finally:
                if token is not None:
                    _end_watermark(token, label)
                    if succeeded:
                        _reclaim_after_active_scope(label)

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
                if _is_active_watermark_prompt():
                    _suspend_prompt_watermark("CUDA OOM surfaced from async node task")
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
        _install_prompt_patch()
        _install_get_output_data_patch()
        _install_async_result_patch()
        status = _guardian_request("status")
        LOG.info("VRAM Guardian plugin loaded; guardian status: %s", status)
    except Exception:
        LOG.exception("failed to install VRAM Guardian plugin")


_install()
