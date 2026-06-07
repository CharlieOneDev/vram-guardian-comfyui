import asyncio
import inspect
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path
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


def _env_has_value(name: str) -> bool:
    value = os.getenv(name)
    return value is not None and value != ""


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None or value == "" else float(value)


def _env_name_set(name: str) -> set[str]:
    return {value.strip() for value in os.getenv(name, "").split(",") if value.strip()}


def _env_pattern_set(name: str, default: set[str]) -> set[str]:
    value = os.getenv(name)
    if value is None:
        return default
    return {part.strip().lower() for part in value.split(",") if part.strip()}


def _env_int_map(name: str) -> dict[str, int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        LOG.warning("invalid %s JSON: %s", name, exc)
        return {}
    if not isinstance(data, dict):
        LOG.warning("%s must be a JSON object", name)
        return {}

    result: dict[str, int] = {}
    for key, value in data.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            LOG.warning("ignoring non-integer %s entry for %s: %r", name, key, value)
    return result


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
SCHEDULER_PRESET = os.getenv("VRAM_GUARDIAN_SCHEDULER_PRESET", "heavy-video").strip().lower()
SCHEDULER_AUTO_PRESET = SCHEDULER_PRESET in {"auto", "default", "heavy", "heavy-video", "video"}
SCHEDULER_OFF_PRESET = SCHEDULER_PRESET in {"0", "false", "manual", "no", "off", "disabled"}
HEAVY_NODES = _env_name_set("VRAM_GUARDIAN_HEAVY_NODES")
DEFAULT_HEAVY_PATTERNS = {
    "bernini",
    "controlnet",
    "decode",
    "dwpose",
    "interpol",
    "loader",
    "ltx",
    "model",
    "pose",
    "preprocessor",
    "rife",
    "rif",
    "sampler",
    "segment",
    "upscale",
    "vae",
    "vsr",
    "wan",
}
HEAVY_PATTERNS = _env_pattern_set("VRAM_GUARDIAN_HEAVY_PATTERNS", DEFAULT_HEAVY_PATTERNS if SCHEDULER_AUTO_PRESET else set())
BASE_FREE_MB_SET = _env_has_value("VRAM_GUARDIAN_BASE_FREE_MB")
BASE_FREE_MB = _env_int("VRAM_GUARDIAN_BASE_FREE_MB", 0)
HEAVY_FREE_MB_SET = _env_has_value("VRAM_GUARDIAN_HEAVY_FREE_MB")
HEAVY_FREE_MB = _env_int("VRAM_GUARDIAN_HEAVY_FREE_MB", ACTIVE_FREE_MB)
AUTO_BASE_FREE_FRACTION = _env_float("VRAM_GUARDIAN_AUTO_BASE_FREE_FRACTION", 0.14)
AUTO_HEAVY_FREE_FRACTION = _env_float("VRAM_GUARDIAN_AUTO_HEAVY_FREE_FRACTION", 0.72)
AUTO_BASE_FREE_CAP_MB = _env_int("VRAM_GUARDIAN_AUTO_BASE_FREE_CAP_MB", 6144)
AUTO_HEAVY_FREE_CAP_MB = _env_int("VRAM_GUARDIAN_AUTO_HEAVY_FREE_CAP_MB", 32768)
AUTO_FREE_RESERVE_MB = _env_int("VRAM_GUARDIAN_AUTO_FREE_RESERVE_MB", 2048)
NODE_FREE_MAP = _env_int_map("VRAM_GUARDIAN_NODE_FREE_MAP")
SCHEDULER_ENABLE = _env_bool(
    "VRAM_GUARDIAN_SCHEDULER_ENABLE",
    not SCHEDULER_OFF_PRESET
    and (SCHEDULER_AUTO_PRESET or BASE_FREE_MB > 0 or HEAVY_FREE_MB > 0 or bool(NODE_FREE_MAP)),
)
SCHEDULER_WAIT_TIMEOUT = _env_float("VRAM_GUARDIAN_WAIT_TIMEOUT_SEC", 120.0)
SCHEDULER_WAIT_POLL = _env_float("VRAM_GUARDIAN_WAIT_POLL_SEC", 0.5)
SCHEDULER_LOG_INTERVAL = _env_float("VRAM_GUARDIAN_WAIT_LOG_INTERVAL_SEC", 5.0)
SCHEDULER_MONITOR_INTERVAL = _env_float("VRAM_GUARDIAN_MONITOR_INTERVAL_SEC", 0.5)
ESTIMATOR_ENABLE = _env_bool("VRAM_GUARDIAN_ESTIMATOR_ENABLE", SCHEDULER_ENABLE and SCHEDULER_AUTO_PRESET)
ESTIMATOR_MARGIN_MB = _env_int("VRAM_GUARDIAN_ESTIMATOR_MARGIN_MB", 2048)
ESTIMATOR_MIN_TARGET_MB = _env_int("VRAM_GUARDIAN_ESTIMATOR_MIN_TARGET_MB", 0)
ESTIMATOR_MAX_FREE_MB = _env_int("VRAM_GUARDIAN_ESTIMATOR_MAX_FREE_MB", 0)
HEAVY_REFILL_MODE = os.getenv("VRAM_GUARDIAN_HEAVY_REFILL_MODE", "no-refill").strip().lower()
HEAVY_ALLOW_REFILL = HEAVY_REFILL_MODE in {"1", "true", "yes", "on", "refill", "fill"}
OOM_BUMP_MB = _env_int("VRAM_GUARDIAN_OOM_BUMP_MB", 4096)
OOM_RETRY_FREE_MB = _env_int("VRAM_GUARDIAN_OOM_RETRY_FREE_MB", 0)
OOM_RETRY_RESERVE_MB = _env_int("VRAM_GUARDIAN_OOM_RETRY_RESERVE_MB", AUTO_FREE_RESERVE_MB)
PROFILE_ENABLE = _env_bool("VRAM_GUARDIAN_PROFILE_ENABLE", SCHEDULER_ENABLE and SCHEDULER_AUTO_PRESET)
PROFILE_PATH = Path(os.getenv("VRAM_GUARDIAN_PROFILE_PATH", "vram_guardian_profile.json"))
PROFILE_MARGIN_MB = _env_int("VRAM_GUARDIAN_PROFILE_MARGIN_MB", 2048)
PROMPT_SCOPES = {"prompt", "workflow", "comfyui"}
NODE_SCOPES = {"node", "nodes"}
_PROMPT_WATERMARK_TOKEN: str | None = None
_PROMPT_WATERMARK_LABEL: str | None = None
_PROMPT_SCOPE_PATCHED = False
_PROFILE_LOCK = threading.RLock()
_PROFILE_DATA: dict[str, Any] = {}
_TOTAL_MB_CACHE: float | None = None


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


def _guardian_status() -> dict[str, Any] | None:
    return _guardian_request("status")


def _status_total_mb(status: dict[str, Any] | None) -> float:
    if not status:
        return 0.0
    try:
        return float(status.get("total_mb", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _guardian_total_mb(status: dict[str, Any] | None = None) -> float:
    global _TOTAL_MB_CACHE

    total_mb = _status_total_mb(status)
    if total_mb > 0:
        _TOTAL_MB_CACHE = total_mb
        return total_mb
    if _TOTAL_MB_CACHE is not None and _TOTAL_MB_CACHE > 0:
        return _TOTAL_MB_CACHE

    total_mb = _status_total_mb(_guardian_status())
    if total_mb > 0:
        _TOTAL_MB_CACHE = total_mb
        return total_mb

    try:
        if torch.cuda.is_available():
            _, total_bytes = torch.cuda.mem_get_info()
            total_mb = total_bytes / (1024 * 1024)
            _TOTAL_MB_CACHE = total_mb
            return total_mb
    except Exception:
        LOG.debug("failed to read local CUDA memory info", exc_info=True)
    return 0.0


def _rounded_target_mb(value: float) -> int:
    if value <= 0:
        return 0
    return max(512, int(value // 512) * 512)


def _auto_free_target_mb(kind: str, status: dict[str, Any] | None = None) -> int:
    total_mb = _guardian_total_mb(status)
    if total_mb <= 0:
        return 0

    if kind == "heavy":
        fraction = AUTO_HEAVY_FREE_FRACTION
        cap_mb = AUTO_HEAVY_FREE_CAP_MB
    else:
        fraction = AUTO_BASE_FREE_FRACTION
        cap_mb = AUTO_BASE_FREE_CAP_MB

    target = max(0.0, total_mb * max(0.0, fraction))
    if cap_mb > 0:
        target = min(target, float(cap_mb))
    if AUTO_FREE_RESERVE_MB > 0:
        target = min(target, max(0.0, total_mb - AUTO_FREE_RESERVE_MB))
    return _rounded_target_mb(target)


def _base_free_target_mb(status: dict[str, Any] | None = None) -> int:
    if BASE_FREE_MB_SET:
        return max(0, BASE_FREE_MB)
    if SCHEDULER_AUTO_PRESET:
        return _auto_free_target_mb("base", status)
    return max(0, BASE_FREE_MB)


def _heavy_free_target_mb(status: dict[str, Any] | None = None) -> int:
    if HEAVY_FREE_MB_SET:
        return max(0, HEAVY_FREE_MB)
    if SCHEDULER_AUTO_PRESET:
        return _auto_free_target_mb("heavy", status)
    return max(0, HEAVY_FREE_MB)


def _ensure_guardian_free(free_mb: int) -> dict[str, Any] | None:
    response = _guardian_request("ensure_free", free_mb=free_mb, pause_refill_sec=RELEASE_REFILL_PAUSE)
    LOG.info("VRAM Guardian ensure_free response: %s", response)
    return response


def _set_watermark(
    mode: bool,
    free_mb: int | None = None,
    hysteresis_mb: int | None = None,
    allow_refill: bool | None = None,
    token: str | None = None,
) -> dict[str, Any] | None:
    fields: dict[str, Any] = {"mode": mode}
    if free_mb is not None:
        fields["free_mb"] = free_mb
    if hysteresis_mb is not None:
        fields["hysteresis_mb"] = hysteresis_mb
    if allow_refill is not None:
        fields["allow_refill"] = allow_refill
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


def _node_input_data(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if "input_data_all" in kwargs:
        return kwargs["input_data_all"]
    if len(args) > 3 and isinstance(args[3], dict):
        return args[3]
    if len(args) > 1 and isinstance(args[1], dict):
        return args[1]
    return {}


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
    return _PROMPT_SCOPE_PATCHED and (
        (
            SCHEDULER_ENABLE
            and (
                (BASE_FREE_MB_SET and BASE_FREE_MB > 0)
                or (not BASE_FREE_MB_SET and SCHEDULER_AUTO_PRESET)
            )
        )
        or (ACTIVE_FREE_MB > 0 and ACTIVE_SCOPE in PROMPT_SCOPES)
    )


def _load_profile() -> None:
    global _PROFILE_DATA

    if not PROFILE_ENABLE:
        _PROFILE_DATA = {"version": 1, "nodes": {}}
        return
    try:
        with PROFILE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        data = {"version": 1, "nodes": {}}
    except Exception as exc:
        LOG.warning("failed to load VRAM Guardian profile %s: %s", PROFILE_PATH, exc)
        data = {"version": 1, "nodes": {}}

    if not isinstance(data, dict):
        data = {"version": 1, "nodes": {}}
    if not isinstance(data.get("nodes"), dict):
        data["nodes"] = {}
    _PROFILE_DATA = data


def _save_profile() -> None:
    if not PROFILE_ENABLE:
        return
    try:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PROFILE_PATH.open("w", encoding="utf-8") as handle:
            json.dump(_PROFILE_DATA, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception as exc:
        LOG.warning("failed to save VRAM Guardian profile %s: %s", PROFILE_PATH, exc)


def _profile_entry(class_name: str) -> dict[str, Any] | None:
    if not PROFILE_ENABLE:
        return None
    with _PROFILE_LOCK:
        nodes = _PROFILE_DATA.setdefault("nodes", {})
        entry = nodes.get(class_name)
        return entry if isinstance(entry, dict) else None


def _profile_target_mb(class_name: str) -> int:
    entry = _profile_entry(class_name)
    if not entry:
        return 0
    try:
        return max(0, int(entry.get("target_free_mb", 0)))
    except (TypeError, ValueError):
        return 0


def _input_signature(input_data_all: Any) -> dict[str, Any]:
    if not isinstance(input_data_all, dict):
        return {}

    signature: dict[str, Any] = {}
    for key, values in input_data_all.items():
        value = values[0] if isinstance(values, (list, tuple)) and values else values
        item: dict[str, Any] = {"type": type(value).__name__}
        shape = getattr(value, "shape", None)
        if shape is not None:
            try:
                item["shape"] = [int(part) for part in shape]
            except Exception:
                item["shape"] = str(shape)
        elif isinstance(value, (str, int, float, bool)):
            item["value"] = value
        elif isinstance(value, (list, tuple)):
            item["length"] = len(value)
        elif isinstance(value, dict):
            item["keys"] = sorted(str(name) for name in value.keys())[:20]
        signature[str(key)] = item
    return signature


def _iter_input_values(input_data_all: Any) -> list[tuple[str, Any]]:
    if not isinstance(input_data_all, dict):
        return []

    items: list[tuple[str, Any]] = []
    for key, values in input_data_all.items():
        if isinstance(values, (list, tuple)):
            for value in values:
                items.append((str(key), value))
        else:
            items.append((str(key), values))
    return items


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _shape_parts(value: Any) -> list[int]:
    shape = getattr(value, "shape", None)
    if shape is None:
        return []
    try:
        return [int(part) for part in shape]
    except Exception:
        return []


def _collect_estimator_features(input_data_all: Any) -> dict[str, Any]:
    width = 0
    height = 0
    frames = 1
    context_frames = 0
    scale = 1.0
    tile_area = 0
    reference_images = 0
    has_large_tensor = False
    tiled = False
    force_offload = False
    text_parts: list[str] = []

    for key, value in _iter_input_values(input_data_all):
        lowered_key = key.lower()
        numeric = _numeric_value(value)
        if numeric is not None:
            if "width" in lowered_key:
                width = max(width, int(numeric))
            elif "height" in lowered_key:
                height = max(height, int(numeric))
            elif any(name in lowered_key for name in ("num_frames", "frame_count", "frames")):
                frames = max(frames, int(numeric))
            elif "context" in lowered_key:
                context_frames = max(context_frames, int(numeric))
            elif any(name in lowered_key for name in ("scale", "upscale", "factor")):
                scale = max(scale, float(numeric))
            elif "tile" in lowered_key:
                tile_area = max(tile_area, int(numeric))

        if isinstance(value, bool):
            if "tile" in lowered_key:
                tiled = tiled or value
            if "offload" in lowered_key:
                force_offload = force_offload or value
        elif isinstance(value, str):
            text_parts.append(value.lower())

        if "reference_image" in lowered_key and value is not None:
            reference_images += 1

        shape = _shape_parts(value)
        if len(shape) >= 4:
            batch = max(1, shape[0])
            if shape[-1] in {1, 3, 4}:
                # IMAGE is usually [N, H, W, C].
                frames = max(frames, batch)
                height = max(height, shape[-3])
                width = max(width, shape[-2])
            else:
                # LATENT is usually [N, C, H, W].
                frames = max(frames, batch)
                height = max(height, shape[-2] * 8)
                width = max(width, shape[-1] * 8)
            has_large_tensor = True
        elif len(shape) == 3:
            if shape[-1] in {1, 3, 4}:
                height = max(height, shape[-3])
                width = max(width, shape[-2])
            else:
                height = max(height, shape[-2])
                width = max(width, shape[-1])
            has_large_tensor = True

    if tile_area > 0:
        tiled = True

    return {
        "width": width,
        "height": height,
        "frames": frames,
        "context_frames": context_frames,
        "scale": scale,
        "reference_images": reference_images,
        "has_large_tensor": has_large_tensor,
        "tiled": tiled,
        "force_offload": force_offload,
        "text": " ".join(text_parts),
    }


def _ceil_target_mb(value: float) -> int:
    if value <= 0:
        return 0
    return max(512, ((int(value) + 511) // 512) * 512)


def _estimate_node_peak_total_mb(class_name: str, input_data_all: Any) -> tuple[int, str, dict[str, Any]]:
    if not ESTIMATOR_ENABLE:
        return 0, "estimator-disabled", {}

    features = _collect_estimator_features(input_data_all)
    lowered = f"{class_name} {features.get('text', '')}".lower()
    width = int(features.get("width", 0) or 0)
    height = int(features.get("height", 0) or 0)
    frames = max(1, int(features.get("frames", 1) or 1))
    context_frames = int(features.get("context_frames", 0) or 0)
    scale = max(1.0, float(features.get("scale", 1.0) or 1.0))
    tiled = bool(features.get("tiled"))
    force_offload = bool(features.get("force_offload"))
    active_frames = frames
    if context_frames > 0 and any(name in lowered for name in ("sampler", "wan", "ltx")):
        active_frames = min(frames, max(1, context_frames))
    elif any(name in lowered for name in ("sampler", "wan", "ltx")):
        active_frames = min(frames, 96)

    mp_frames = (max(width, 1) * max(height, 1) * max(active_frames, 1)) / 1_000_000
    all_mp_frames = (max(width, 1) * max(height, 1) * max(frames, 1)) / 1_000_000
    has_size = width > 0 and height > 0
    peak = 0.0
    source = ""

    if any(name in lowered for name in ("wan", "ltx", "sampler")):
        if has_size or features.get("has_large_tensor"):
            peak = 22000 + mp_frames * 430
            source = "estimate-video-sampler"
        else:
            return 0, "estimate-insufficient-inputs", {}
    elif any(name in lowered for name in ("bernini", "image_embed", "imageembeds")):
        if has_size or features.get("has_large_tensor"):
            peak = 14000 + all_mp_frames * 150 + int(features.get("reference_images", 0) or 0) * 512
            source = "estimate-embeds"
        else:
            return 0, "estimate-insufficient-inputs", {}
    elif any(name in lowered for name in ("decode", "vae")):
        if has_size or features.get("has_large_tensor"):
            coeff = 45 if tiled else 160
            peak = 9000 + all_mp_frames * coeff
            source = "estimate-vae"
        else:
            return 0, "estimate-insufficient-inputs", {}
    elif any(name in lowered for name in ("vsr", "upscale", "interpol", "rife", "segment")):
        if has_size or features.get("has_large_tensor"):
            peak = 12000 + all_mp_frames * (scale * scale) * 150
            source = "estimate-video-post"
        else:
            return 0, "estimate-insufficient-inputs", {}
    elif any(name in lowered for name in ("pose", "controlnet", "preprocessor", "dwpose")):
        if has_size or features.get("has_large_tensor"):
            peak = 6000 + all_mp_frames * 75
            source = "estimate-preprocess"
        else:
            return 0, "estimate-insufficient-inputs", {}
    elif any(name in lowered for name in ("loader", "model")):
        peak = 14000
        if any(name in lowered for name in ("bf16", "fp16", "float16")):
            peak += 6000
        if any(name in lowered for name in ("fp8", "int8", "gguf")):
            peak -= 2500
        source = "estimate-model"
    elif features.get("has_large_tensor"):
        peak = 5000 + all_mp_frames * 100
        source = "estimate-shape"

    if force_offload and peak > 0:
        peak *= 0.85

    total_mb = _guardian_total_mb()
    if total_mb > 0 and peak > 0:
        peak = min(peak, max(0.0, total_mb - AUTO_FREE_RESERVE_MB))

    details = {
        "width": width,
        "height": height,
        "frames": frames,
        "active_frames": active_frames,
        "mp_frames": round(mp_frames, 2),
        "all_mp_frames": round(all_mp_frames, 2),
        "tiled": tiled,
        "force_offload": force_offload,
    }
    return _ceil_target_mb(peak), source, details


def _estimated_target_free_mb(class_name: str, input_data_all: Any, status: dict[str, Any] | None) -> tuple[int, str]:
    peak_total_mb, source, details = _estimate_node_peak_total_mb(class_name, input_data_all)
    if peak_total_mb <= 0:
        return 0, source

    current_comfyui_mb = _status_comfyui_used_mb(status)
    raw_target = max(0.0, peak_total_mb - current_comfyui_mb + ESTIMATOR_MARGIN_MB)
    target = max(raw_target, float(ESTIMATOR_MIN_TARGET_MB), float(_base_free_target_mb(status)))
    total_mb = _guardian_total_mb(status)
    if ESTIMATOR_MAX_FREE_MB > 0:
        target = min(target, float(ESTIMATOR_MAX_FREE_MB))
    elif total_mb > 0:
        target = min(target, max(0.0, total_mb - AUTO_FREE_RESERVE_MB))

    target_mb = _ceil_target_mb(target)
    LOG.info(
        "[VRAM Estimator] class=%s peak_total=%sMiB current_comfyui=%.0fMiB target_free=%sMiB source=%s details=%s",
        class_name,
        peak_total_mb,
        current_comfyui_mb,
        target_mb,
        source,
        details,
    )
    return target_mb, source


def _record_profile(run: "SchedulerRun", *, oom: bool = False, success: bool = False, bump: bool = False) -> None:
    if not PROFILE_ENABLE:
        return

    duration = max(0.0, time.monotonic() - run.started_at)
    min_free = run.min_free_mb if run.min_free_mb is not None else run.start_free_mb
    peak_need = max(0, int((run.start_free_mb or run.target_free_mb) - (min_free or 0)))
    learned_target = max(run.target_free_mb, peak_need + PROFILE_MARGIN_MB)
    if bump:
        learned_target = max(learned_target, run.target_free_mb + OOM_BUMP_MB)

    with _PROFILE_LOCK:
        nodes = _PROFILE_DATA.setdefault("nodes", {})
        entry = nodes.setdefault(run.class_name, {})
        current_target = int(entry.get("target_free_mb", 0) or 0)
        entry["target_free_mb"] = max(current_target, learned_target)
        entry["runs"] = int(entry.get("runs", 0) or 0) + (1 if success else 0)
        entry["ooms"] = int(entry.get("ooms", 0) or 0) + (1 if oom else 0)
        entry["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entry["last"] = {
            "node_id": run.node_id,
            "target_free_mb": run.target_free_mb,
            "start_free_mb": run.start_free_mb,
            "min_free_mb": min_free,
            "end_free_mb": run.end_free_mb,
            "peak_need_mb": peak_need,
            "duration_sec": round(duration, 3),
            "oom": oom,
            "success": success,
            "input_signature": run.input_signature,
        }
        _save_profile()


def _record_oom_bump(
    label: str,
    class_name: str,
    node_id: Any,
    target_free_mb: int,
    input_signature: dict[str, Any],
) -> None:
    if not PROFILE_ENABLE:
        return
    run = SchedulerRun(label, class_name, node_id, max(target_free_mb, _base_free_target_mb(), ACTIVE_FREE_MB), input_signature)
    run.start(_guardian_status())
    run.stop()
    _record_profile(run, oom=True, success=False, bump=True)


def _scheduler_prompt_target_mb() -> int:
    if SCHEDULER_ENABLE:
        target = _base_free_target_mb()
        if target > 0:
            return target
    if ACTIVE_FREE_MB > 0 and ACTIVE_SCOPE in PROMPT_SCOPES:
        return ACTIVE_FREE_MB
    return 0


def _matches_heavy_pattern(class_name: str) -> bool:
    normalized = class_name.lower()
    return any(pattern and pattern in normalized for pattern in HEAVY_PATTERNS)


def _scheduler_node_target_mb(class_name: str, input_data_all: Any | None = None) -> tuple[int, str]:
    if not SCHEDULER_ENABLE:
        if ACTIVE_FREE_MB > 0 and ACTIVE_SCOPE in NODE_SCOPES:
            return ACTIVE_FREE_MB, "legacy-active"
        return 0, "disabled"

    target = 0
    source = "none"
    if class_name in NODE_FREE_MAP:
        target = NODE_FREE_MAP[class_name]
        source = "node-map"
    else:
        status = _guardian_status() if ESTIMATOR_ENABLE else None
        estimated_target, estimated_source = _estimated_target_free_mb(class_name, input_data_all or {}, status)
        if estimated_target > 0:
            target = estimated_target
            source = estimated_source
        elif class_name in HEAVY_NODES:
            target = _heavy_free_target_mb(status)
            source = "heavy"
        elif _matches_heavy_pattern(class_name):
            target = _heavy_free_target_mb(status)
            source = "heavy-pattern"
        elif _PROMPT_WATERMARK_TOKEN is None and _base_free_target_mb(status) > 0:
            target = _base_free_target_mb(status)
            source = "base"

    profile_target = _profile_target_mb(class_name)
    if profile_target > target:
        target = profile_target
        source = "profile"

    return max(0, target), source


def _node_watermark_allow_refill(target_free_mb: int, target_source: str) -> bool:
    base_target = _base_free_target_mb()
    if target_source in {"base", "legacy-active", "disabled", "none"}:
        return True
    if target_free_mb <= base_target + max(512, ACTIVE_HYSTERESIS_MB):
        return True
    return HEAVY_ALLOW_REFILL


def _status_free_mb(status: dict[str, Any] | None) -> float:
    if not status:
        return 0.0
    try:
        return float(status.get("free_mb", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _status_held_mb(status: dict[str, Any] | None) -> float:
    if not status:
        return 0.0
    try:
        return float(status.get("held_mb", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _status_comfyui_used_mb(status: dict[str, Any] | None) -> float:
    values: list[float] = []
    if status:
        try:
            value = float(status.get("comfyui_used_mb", 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            values.append(value)
    try:
        if torch.cuda.is_available():
            values.append(float(torch.cuda.memory_reserved()) / (1024 * 1024))
            values.append(float(torch.cuda.memory_allocated()) / (1024 * 1024))
    except Exception:
        LOG.debug("failed to read local torch CUDA memory", exc_info=True)
    return max(values) if values else 0.0


def _wait_for_free_mb(label: str, target_free_mb: int) -> dict[str, Any] | None:
    if target_free_mb <= 0:
        return _guardian_status()

    deadline = None if SCHEDULER_WAIT_TIMEOUT <= 0 else time.monotonic() + SCHEDULER_WAIT_TIMEOUT
    next_log = 0.0
    status = _ensure_guardian_free(target_free_mb)

    while True:
        free_mb = _status_free_mb(status)
        if free_mb >= target_free_mb:
            LOG.info("[VRAM Scheduler] %s free reached %.0fMiB target=%sMiB; continuing", label, free_mb, target_free_mb)
            return status

        now = time.monotonic()
        held_mb = _status_held_mb(status)
        if now >= next_log:
            LOG.info(
                "[VRAM Scheduler] %s waiting: free=%.0fMiB target=%sMiB guardian_held=%.0fMiB",
                label,
                free_mb,
                target_free_mb,
                held_mb,
            )
            if held_mb <= 0:
                LOG.warning(
                    "[VRAM Scheduler] %s Guardian holds no VRAM but free is still below target; another process may be using the GPU",
                    label,
                )
            next_log = now + max(1.0, SCHEDULER_LOG_INTERVAL)

        if deadline is not None and now >= deadline:
            LOG.warning(
                "[VRAM Scheduler] %s wait timed out after %.1fs: free=%.0fMiB target=%sMiB",
                label,
                SCHEDULER_WAIT_TIMEOUT,
                free_mb,
                target_free_mb,
            )
            return status

        time.sleep(max(0.05, SCHEDULER_WAIT_POLL))
        status = _ensure_guardian_free(target_free_mb)


def _oom_retry_target_mb(label: str, current_target_mb: int) -> int:
    status = _guardian_status()
    total_mb = _guardian_total_mb(status)
    comfyui_mb = _status_comfyui_used_mb(status)
    if OOM_RETRY_FREE_MB > 0:
        requested = float(OOM_RETRY_FREE_MB)
        source = "explicit"
    else:
        requested = float(max(current_target_mb + max(0, OOM_BUMP_MB), _heavy_free_target_mb(status)))
        source = "auto"

    cap = 0.0
    if total_mb > 0:
        cap = max(0.0, total_mb - max(0, OOM_RETRY_RESERVE_MB) - comfyui_mb)
        requested = min(requested, cap)

    target = _rounded_target_mb(requested)
    LOG.info(
        "[VRAM Scheduler] %s OOM retry target_free=%sMiB source=%s current_target=%sMiB "
        "current_comfyui=%.0fMiB total=%.0fMiB cap=%.0fMiB reserve=%sMiB",
        label,
        target,
        source,
        current_target_mb,
        comfyui_mb,
        total_mb,
        cap,
        OOM_RETRY_RESERVE_MB,
    )
    return target


def _prepare_oom_retry(label: str, current_target_mb: int) -> None:
    _release_guardian()
    _local_cuda_cleanup()
    retry_target = _oom_retry_target_mb(label, current_target_mb)
    if retry_target > 0:
        _wait_for_free_mb(f"{label} OOM retry", retry_target)


class SchedulerRun:
    def __init__(self, label: str, class_name: str, node_id: Any, target_free_mb: int, input_signature: dict[str, Any]) -> None:
        self.label = label
        self.class_name = class_name
        self.node_id = None if node_id is None else str(node_id)
        self.target_free_mb = target_free_mb
        self.input_signature = input_signature
        self.started_at = time.monotonic()
        self.start_free_mb = 0.0
        self.min_free_mb: float | None = None
        self.end_free_mb: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, initial_status: dict[str, Any] | None = None) -> None:
        self.sample(initial_status or _guardian_status())
        self.start_free_mb = self.min_free_mb or 0.0
        if SCHEDULER_MONITOR_INTERVAL <= 0:
            return
        self._thread = threading.Thread(target=self._loop, name=f"vram-scheduler-{self.class_name}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(max(0.1, SCHEDULER_MONITOR_INTERVAL)):
            self.sample(_guardian_status())

    def sample(self, status: dict[str, Any] | None) -> None:
        free = _status_free_mb(status)
        if self.min_free_mb is None or free < self.min_free_mb:
            self.min_free_mb = free
        self.end_free_mb = free

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, SCHEDULER_MONITOR_INTERVAL * 2))
        self.sample(_guardian_status())


def _watermark_token(label: str) -> str:
    return f"{os.getpid()}:{label}:{time.monotonic_ns()}"


def _begin_watermark(label: str, free_mb: int, hysteresis_mb: int = ACTIVE_HYSTERESIS_MB, allow_refill: bool = True) -> str:
    token = _watermark_token(label)
    LOG.info(
        "enabling Guardian active watermark for %s: free=%sMB hysteresis=%sMB allow_refill=%s token=%s",
        label,
        free_mb,
        hysteresis_mb,
        allow_refill,
        token,
    )
    _set_watermark(True, free_mb, hysteresis_mb, allow_refill=allow_refill, token=token)
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
        if _PROMPT_WATERMARK_TOKEN is not None:
            LOG.info("prompt watermark is still active after %s; skipping full reclaim", label)
            return
        LOG.info("reclaiming Guardian VRAM after active watermark scope %s", label)
        _reclaim_guardian()


def _watch_pending_tasks(tasks: list[asyncio.Task[Any]], token: str, label: str, run: SchedulerRun | None = None) -> None:
    async def wait_and_close() -> None:
        failed = True
        oom_failed = False
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failed = any(isinstance(result, BaseException) for result in results)
            oom_failed = any(isinstance(result, BaseException) and _is_oom(result) for result in results)
            if failed:
                LOG.warning("pending tasks for %s ended with an error; Guardian will not reclaim immediately", label)
        finally:
            if run is not None:
                run.stop()
                _record_profile(run, oom=oom_failed, success=not failed, bump=oom_failed)
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
            target_free_mb = _scheduler_prompt_target_mb()
            if target_free_mb > 0:
                token = _begin_watermark(label, target_free_mb)
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
        target_free_mb = _scheduler_prompt_target_mb()
        if target_free_mb > 0:
            token = _begin_watermark(label, target_free_mb)
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
            obj = _node_obj(args, kwargs)
            class_name = _node_class_name(obj)
            node_id = _node_unique_id(args, kwargs, obj)
            label = _node_label(args, kwargs)
            input_data = _node_input_data(args, kwargs)
            input_signature = _input_signature(input_data)
            target_free_mb, target_source = _scheduler_node_target_mb(class_name, input_data)
            active_scheduler = target_free_mb > 0
            allow_refill = _node_watermark_allow_refill(target_free_mb, target_source)
            if active_scheduler or ACTIVE_SCOPE in NODE_SCOPES:
                LOG.info(
                    "[VRAM Scheduler] node=%s class=%s target_free=%sMiB source=%s allow_refill=%s",
                    label,
                    class_name,
                    target_free_mb,
                    target_source,
                    allow_refill,
                )
            else:
                LOG.debug("[VRAM Scheduler] node=%s class=%s target_free=0 source=%s", label, class_name, target_source)
            full_release_retry = False
            for attempt in range(MAX_RETRY + 1):
                token: str | None = None
                run: SchedulerRun | None = None
                succeeded = False
                profile_recorded = False
                try:
                    if active_scheduler and not full_release_retry:
                        token = _begin_watermark(label, target_free_mb, allow_refill=allow_refill)
                        status = _wait_for_free_mb(label, target_free_mb)
                        run = SchedulerRun(label, class_name, node_id, target_free_mb, input_signature)
                        run.start(status)
                    elif active_scheduler and full_release_retry:
                        LOG.info("[VRAM Scheduler] running full-release retry for node %s without watermark refill", label)
                    if RELEASE_BEFORE_NODE and not active_scheduler and not _is_active_watermark_prompt() and attempt == 0:
                        LOG.info("releasing Guardian VRAM before node %s", label)
                        _release_guardian()
                        _local_cuda_cleanup()
                        await _async_sleep()
                    result = await original(*args, **kwargs)
                    succeeded = True
                    if active_scheduler and token is not None:
                        pending_tasks = _pending_tasks_from_result(result)
                        if pending_tasks:
                            LOG.info(
                                "keeping Guardian active watermark for node %s until %s pending task(s) finish",
                                label,
                                len(pending_tasks),
                            )
                            _watch_pending_tasks(pending_tasks, token, label, run)
                            token = None
                            run = None
                            profile_recorded = True
                    elif active_scheduler and full_release_retry:
                        _reclaim_after_active_scope(label)
                    elif RECLAIM_ON_SUCCESS and not _is_active_watermark_prompt():
                        _reclaim_guardian()
                    return result
                except Exception as exc:
                    oom = _is_oom(exc)
                    if not oom or attempt >= MAX_RETRY:
                        if run is not None:
                            run.stop()
                            _record_profile(run, oom=oom, success=False, bump=oom)
                            profile_recorded = True
                        raise
                    LOG.warning(
                        "CUDA OOM in %s; releasing Guardian VRAM and retrying (%s/%s)",
                        label,
                        attempt + 1,
                        MAX_RETRY,
                    )
                    if run is not None:
                        run.stop()
                        _record_profile(run, oom=True, success=False, bump=True)
                        profile_recorded = True
                        run = None
                    else:
                        _record_oom_bump(label, class_name, node_id, target_free_mb, input_signature)
                    if token is not None:
                        _end_watermark(token, label)
                        token = None
                    if _is_active_watermark_prompt():
                        _suspend_prompt_watermark(f"CUDA OOM in {label}")
                    full_release_retry = True
                    _prepare_oom_retry(label, target_free_mb)
                    await _async_sleep()
                finally:
                    if run is not None and not profile_recorded:
                        run.stop()
                        if succeeded:
                            _record_profile(run, success=True)
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
        obj = _node_obj(args, kwargs)
        class_name = _node_class_name(obj)
        node_id = _node_unique_id(args, kwargs, obj)
        label = _node_label(args, kwargs)
        input_data = _node_input_data(args, kwargs)
        input_signature = _input_signature(input_data)
        target_free_mb, target_source = _scheduler_node_target_mb(class_name, input_data)
        active_scheduler = target_free_mb > 0
        allow_refill = _node_watermark_allow_refill(target_free_mb, target_source)
        if active_scheduler or ACTIVE_SCOPE in NODE_SCOPES:
            LOG.info(
                "[VRAM Scheduler] node=%s class=%s target_free=%sMiB source=%s allow_refill=%s",
                label,
                class_name,
                target_free_mb,
                target_source,
                allow_refill,
            )
        else:
            LOG.debug("[VRAM Scheduler] node=%s class=%s target_free=0 source=%s", label, class_name, target_source)
        full_release_retry = False
        for attempt in range(MAX_RETRY + 1):
            token: str | None = None
            run: SchedulerRun | None = None
            succeeded = False
            profile_recorded = False
            try:
                if active_scheduler and not full_release_retry:
                    token = _begin_watermark(label, target_free_mb, allow_refill=allow_refill)
                    status = _wait_for_free_mb(label, target_free_mb)
                    run = SchedulerRun(label, class_name, node_id, target_free_mb, input_signature)
                    run.start(status)
                elif active_scheduler and full_release_retry:
                    LOG.info("[VRAM Scheduler] running full-release retry for node %s without watermark refill", label)
                if RELEASE_BEFORE_NODE and not active_scheduler and not _is_active_watermark_prompt() and attempt == 0:
                    LOG.info("releasing Guardian VRAM before node %s", label)
                    _release_guardian()
                    _local_cuda_cleanup()
                    if RETRY_SLEEP > 0:
                        time.sleep(RETRY_SLEEP)
                result = original(*args, **kwargs)
                succeeded = True
                if active_scheduler and full_release_retry:
                    _reclaim_after_active_scope(label)
                elif RECLAIM_ON_SUCCESS and not active_scheduler and not _is_active_watermark_prompt():
                    _reclaim_guardian()
                return result
            except Exception as exc:
                oom = _is_oom(exc)
                if not oom or attempt >= MAX_RETRY:
                    if run is not None:
                        run.stop()
                        _record_profile(run, oom=oom, success=False, bump=oom)
                        profile_recorded = True
                    raise
                LOG.warning(
                    "CUDA OOM in %s; releasing Guardian VRAM and retrying (%s/%s)",
                    label,
                    attempt + 1,
                    MAX_RETRY,
                )
                if run is not None:
                    run.stop()
                    _record_profile(run, oom=True, success=False, bump=True)
                    profile_recorded = True
                    run = None
                else:
                    _record_oom_bump(label, class_name, node_id, target_free_mb, input_signature)
                if token is not None:
                    _end_watermark(token, label)
                    token = None
                if _is_active_watermark_prompt():
                    _suspend_prompt_watermark(f"CUDA OOM in {label}")
                full_release_retry = True
                _prepare_oom_retry(label, target_free_mb)
                if RETRY_SLEEP > 0:
                    time.sleep(RETRY_SLEEP)
            finally:
                if run is not None and not profile_recorded:
                    run.stop()
                    if succeeded:
                        _record_profile(run, success=True)
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
        _load_profile()
        _install_prompt_patch()
        _install_get_output_data_patch()
        _install_async_result_patch()
        status = _guardian_request("status")
        LOG.info(
            "VRAM Guardian scheduler config: enabled=%s preset=%s base_free=%sMiB heavy_free=%sMiB "
            "estimator=%s estimator_margin=%sMiB heavy_refill_mode=%s profile=%s heavy_patterns=%s node_map=%s",
            SCHEDULER_ENABLE,
            SCHEDULER_PRESET,
            _base_free_target_mb(status),
            _heavy_free_target_mb(status),
            ESTIMATOR_ENABLE,
            ESTIMATOR_MARGIN_MB,
            HEAVY_REFILL_MODE,
            PROFILE_ENABLE,
            ",".join(sorted(HEAVY_PATTERNS)) or "-",
            ",".join(sorted(NODE_FREE_MAP)) or "-",
        )
        LOG.info("VRAM Guardian plugin loaded; guardian status: %s", status)
    except Exception:
        LOG.exception("failed to install VRAM Guardian plugin")


_install()
