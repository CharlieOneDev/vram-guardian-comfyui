import argparse
import gc
import json
import logging
import os
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import torch


MIB = 1024 * 1024
LOG = logging.getLogger("vram_guardian")


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def query_gpu_processes() -> list[dict[str, Any]]:
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except Exception:
        return []

    processes: list[dict[str, Any]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) != 3:
            continue
        try:
            pid = int(parts[0])
            used_mb = float(parts[2].split()[0])
        except ValueError:
            continue

        cmdline = read_process_cmdline(pid)
        cwd = read_process_cwd(pid)
        processes.append(
            {
                "pid": pid,
                "process_name": parts[1],
                "used_mb": round(used_mb, 2),
                "role": classify_process(pid, parts[1], cmdline, cwd),
                "cmdline": compact_command(cmdline or parts[1]),
                "cwd": cwd,
            }
        )
    return processes


def read_process_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def read_process_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def classify_process(pid: int, process_name: str, cmdline: str, cwd: str) -> str:
    comfyui_pid = os.getenv("VRAM_GUARDIAN_COMFYUI_PID", "").strip()
    if comfyui_pid and comfyui_pid == str(pid):
        return "comfyui"
    if pid == os.getpid() or "vram_guardian.server" in cmdline:
        return "guardian"

    text = f"{process_name} {cmdline} {cwd}"
    if "/workspace/ComfyUI" in text or "ComfyUI/main.py" in text:
        return "comfyui"
    if cwd.endswith("/ComfyUI") and "python" in process_name.lower():
        return "comfyui"
    return "other"


def compact_command(command: str, limit: int = 160) -> str:
    command = " ".join(command.split())
    if len(command) <= limit:
        return command
    return command[: limit - 3] + "..."


@dataclass
class GuardianConfig:
    device: str = "cuda:0"
    fraction: float = 0.98
    min_free_mb: int = 0
    chunk_mb: int = 256
    max_hold_mb: int = 0
    auto_refill: bool = False
    auto_refill_interval_sec: float = 5.0
    auto_refill_min_delta_mb: int = 256
    watermark_mode: bool = False
    watermark_free_mb: int = 0
    watermark_hysteresis_mb: int = 2048
    watermark_allow_refill: bool = True
    watermark_interval_sec: float = 1.0
    watermark_release_cooldown_sec: float = 5.0

    @property
    def min_free_bytes(self) -> int:
        return max(0, self.min_free_mb) * MIB

    @property
    def chunk_bytes(self) -> int:
        return max(16, self.chunk_mb) * MIB

    @property
    def max_hold_bytes(self) -> int:
        return max(0, self.max_hold_mb) * MIB

    @property
    def auto_refill_min_delta_bytes(self) -> int:
        return max(16, self.auto_refill_min_delta_mb) * MIB


class VramGuardian:
    def __init__(self, config: GuardianConfig) -> None:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available in this container")

        self.config = config
        self.device = torch.device(config.device)
        torch.cuda.set_device(self.device)

        self.lock = threading.RLock()
        self.chunks: list[torch.Tensor] = []
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.auto_refill_thread: threading.Thread | None = None
        self.auto_refill_paused_until = 0.0
        self.manual_watermark_mode = config.watermark_mode
        self.manual_watermark_free_mb = config.watermark_free_mb
        self.manual_watermark_hysteresis_mb = config.watermark_hysteresis_mb
        self.manual_watermark_allow_refill = config.watermark_allow_refill
        self.watermark_sessions: dict[str, tuple[int, int, bool]] = {}
        self.watermark_cooldown_until = 0.0

    def held_bytes_unlocked(self) -> int:
        return sum(chunk.numel() * chunk.element_size() for chunk in self.chunks)

    def mem_info_unlocked(self) -> tuple[int, int]:
        free, total = torch.cuda.mem_get_info(self.device)
        return int(free), int(total)

    def target_used_bytes_unlocked(self) -> int:
        _, total = self.mem_info_unlocked()
        target = int(total * max(0.0, min(0.98, self.config.fraction)))
        return min(target, max(0, total - self.config.min_free_bytes))

    def target_bytes_unlocked(self) -> int:
        free, total = self.mem_info_unlocked()
        held = self.held_bytes_unlocked()
        external_used = max(0, total - free - held)
        target = max(0, self.target_used_bytes_unlocked() - external_used)
        if self.config.max_hold_bytes > 0:
            target = min(target, self.config.max_hold_bytes)
        return max(0, target)

    def fill(self, clear_pause: bool = True) -> dict[str, Any]:
        with self.lock:
            if clear_pause:
                self.auto_refill_paused_until = 0.0
            allocated = self.fill_preserving_free_unlocked(self.config.min_free_bytes)

            return self.status_unlocked(extra={"allocated_bytes": allocated})

    def fill_preserving_free_unlocked(self, min_free_bytes: int) -> int:
        allocated = 0
        attempts = 0
        while True:
            held = self.held_bytes_unlocked()
            free, _ = self.mem_info_unlocked()
            target = self.target_bytes_unlocked()
            remaining_to_target = target - held
            available_to_take = free - min_free_bytes

            if remaining_to_target <= 0 or available_to_take < 16 * MIB:
                break

            size = min(self.config.chunk_bytes, remaining_to_target, available_to_take)
            size = int(size // MIB) * MIB
            if size < 16 * MIB:
                break

            chunk = None
            try:
                chunk = torch.empty(size, dtype=torch.uint8, device=self.device)
                chunk.zero_()
                torch.cuda.synchronize(self.device)
                self.chunks.append(chunk)
                allocated += size
                attempts = 0
            except torch.cuda.OutOfMemoryError:
                if chunk is not None:
                    del chunk
                attempts += 1
                torch.cuda.empty_cache()
                if attempts >= 3 or size <= 16 * MIB:
                    break
                self.config.chunk_mb = max(16, self.config.chunk_mb // 2)
                LOG.warning("OOM while filling; reducing chunk size to %s MiB", self.config.chunk_mb)
        return allocated

    def reserve_bytes_unlocked(self, bytes_to_reserve: int, min_free_bytes: int) -> int:
        allocated = 0
        attempts = 0
        while allocated < bytes_to_reserve:
            held = self.held_bytes_unlocked()
            free, _ = self.mem_info_unlocked()
            target = self.target_bytes_unlocked()
            available_to_take = free - min_free_bytes
            remaining = bytes_to_reserve - allocated
            remaining_to_target = target - held
            remaining_hold = self.config.max_hold_bytes - held if self.config.max_hold_bytes > 0 else remaining

            if remaining <= 0 or remaining_to_target <= 0 or remaining_hold <= 0 or available_to_take < 16 * MIB:
                break

            size = min(self.config.chunk_bytes, remaining, remaining_to_target, remaining_hold, available_to_take)
            size = int(size // MIB) * MIB
            if size < 16 * MIB:
                break

            chunk = None
            try:
                chunk = torch.empty(size, dtype=torch.uint8, device=self.device)
                chunk.zero_()
                torch.cuda.synchronize(self.device)
                self.chunks.append(chunk)
                allocated += size
                attempts = 0
            except torch.cuda.OutOfMemoryError:
                if chunk is not None:
                    del chunk
                attempts += 1
                torch.cuda.empty_cache()
                if attempts >= 3 or size <= 16 * MIB:
                    break
                self.config.chunk_mb = max(16, self.config.chunk_mb // 2)
                LOG.warning("OOM while reserving; reducing chunk size to %s MiB", self.config.chunk_mb)
        return allocated

    def reserve(self, bytes_to_reserve: int) -> dict[str, Any]:
        with self.lock:
            if bytes_to_reserve <= 0:
                allocated = self.fill_preserving_free_unlocked(self.config.min_free_bytes)
                command = "fill"
            else:
                allocated = self.reserve_bytes_unlocked(bytes_to_reserve, self.config.min_free_bytes)
                command = "reserve"
            status = self.status_unlocked(extra={"allocated_bytes": allocated, "manual_command": command})
            LOG.info(
                "%s allocated %.2f MiB | %s",
                command,
                allocated / MIB,
                self.format_memory_summary(status),
            )
            return status

    def release_bytes_unlocked(self, bytes_to_release: int = 0) -> int:
        released = 0
        if bytes_to_release <= 0:
            released = self.held_bytes_unlocked()
            self.chunks.clear()
        else:
            while self.chunks and released < bytes_to_release:
                chunk = self.chunks.pop()
                released += chunk.numel() * chunk.element_size()
                del chunk
        return released

    def pause_auto_refill(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self.lock:
            self.auto_refill_paused_until = max(self.auto_refill_paused_until, time.monotonic() + seconds)

    def maybe_refill(self) -> dict[str, Any]:
        with self.lock:
            if self.config.watermark_mode:
                return self.apply_watermark_unlocked()
            if not self.config.auto_refill:
                return self.status_unlocked(extra={"allocated_bytes": 0, "auto_refill_skipped": "disabled"})
            paused_for = self.auto_refill_paused_until - time.monotonic()
            if paused_for > 0:
                return self.status_unlocked(
                    extra={"allocated_bytes": 0, "auto_refill_skipped": "paused", "auto_refill_paused_sec": round(paused_for, 2)}
                )
            held = self.held_bytes_unlocked()
            free, _ = self.mem_info_unlocked()
            target = self.target_bytes_unlocked()
            remaining_to_target = target - held
            available_to_take = free - self.config.min_free_bytes
            if remaining_to_target < self.config.auto_refill_min_delta_bytes:
                return self.status_unlocked(extra={"allocated_bytes": 0, "auto_refill_skipped": "target"})
            if available_to_take < self.config.auto_refill_min_delta_bytes:
                return self.status_unlocked(extra={"allocated_bytes": 0, "auto_refill_skipped": "free"})
        return self.fill(clear_pause=False)

    def release(self, bytes_to_release: int = 0, pause_refill_sec: float = 0.0) -> dict[str, Any]:
        with self.lock:
            if pause_refill_sec > 0:
                self.auto_refill_paused_until = max(self.auto_refill_paused_until, time.monotonic() + pause_refill_sec)
            released = self.release_bytes_unlocked(bytes_to_release)
            if released > 0 and self.config.watermark_mode:
                self.watermark_cooldown_until = time.monotonic() + max(0.0, self.config.watermark_release_cooldown_sec)
            self.wake_event.set()

            gc.collect()
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                LOG.debug("torch.cuda.ipc_collect failed", exc_info=True)

            status = self.status_unlocked(extra={"released_bytes": released})
            LOG.info(
                "release freed %.2f MiB pause_refill=%.1fs | %s",
                released / MIB,
                pause_refill_sec,
                self.format_memory_summary(status),
            )
            return status

    def ensure_free(self, free_mb: int, pause_refill_sec: float = 0.0) -> dict[str, Any]:
        with self.lock:
            target_bytes = max(0, int(free_mb)) * MIB
            free, _ = self.mem_info_unlocked()
            needed = max(0, target_bytes - free)
            if pause_refill_sec > 0:
                self.auto_refill_paused_until = max(self.auto_refill_paused_until, time.monotonic() + pause_refill_sec)
            released = self.release_bytes_unlocked(needed)
            if released > 0 and self.config.watermark_mode:
                self.watermark_cooldown_until = time.monotonic() + max(0.0, self.config.watermark_release_cooldown_sec)
            self.wake_event.set()

            if released > 0:
                gc.collect()
                torch.cuda.empty_cache()
                try:
                    torch.cuda.ipc_collect()
                except Exception:
                    LOG.debug("torch.cuda.ipc_collect failed", exc_info=True)

            status = self.status_unlocked(
                extra={
                    "requested_free_mb": free_mb,
                    "released_bytes": released,
                    "watermark_action": "ensure_free",
                }
            )
            LOG.info(
                "ensure_free target=%sMiB released=%.2f MiB pause_refill=%.1fs | %s",
                free_mb,
                released / MIB,
                pause_refill_sec,
                self.format_memory_summary(status),
            )
            return status

    def set_watermark(
        self,
        mode: bool,
        free_mb: int | None = None,
        hysteresis_mb: int | None = None,
        allow_refill: bool | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if token:
                if mode:
                    session_free_mb = max(0, free_mb if free_mb is not None else self.config.watermark_free_mb)
                    session_hysteresis_mb = max(16, hysteresis_mb if hysteresis_mb is not None else self.config.watermark_hysteresis_mb)
                    session_allow_refill = self.config.watermark_allow_refill if allow_refill is None else bool(allow_refill)
                    self.watermark_sessions[token] = (session_free_mb, session_hysteresis_mb, session_allow_refill)
                else:
                    self.watermark_sessions.pop(token, None)
            else:
                self.manual_watermark_mode = mode
                if free_mb is not None:
                    self.manual_watermark_free_mb = max(0, free_mb)
                if hysteresis_mb is not None:
                    self.manual_watermark_hysteresis_mb = max(16, hysteresis_mb)
                if allow_refill is not None:
                    self.manual_watermark_allow_refill = bool(allow_refill)

            self.refresh_watermark_config_unlocked()
            self.wake_event.set()
            status = self.apply_watermark_unlocked() if self.config.watermark_mode else self.status_unlocked()
            LOG.info(
                "watermark mode=%s free_target=%sMiB hysteresis=%sMiB allow_refill=%s sessions=%s token=%s | %s",
                self.config.watermark_mode,
                self.config.watermark_free_mb,
                self.config.watermark_hysteresis_mb,
                self.config.watermark_allow_refill,
                len(self.watermark_sessions),
                token or "manual",
                self.format_memory_summary(status),
            )
            return status

    def refresh_watermark_config_unlocked(self) -> None:
        if self.watermark_sessions:
            self.config.watermark_mode = True
            self.config.watermark_free_mb = max(free_mb for free_mb, _, _ in self.watermark_sessions.values())
            self.config.watermark_hysteresis_mb = max(hysteresis_mb for _, hysteresis_mb, _ in self.watermark_sessions.values())
            self.config.watermark_allow_refill = all(allow_refill for _, _, allow_refill in self.watermark_sessions.values())
            return

        self.config.watermark_mode = self.manual_watermark_mode
        self.config.watermark_free_mb = self.manual_watermark_free_mb
        self.config.watermark_hysteresis_mb = self.manual_watermark_hysteresis_mb
        self.config.watermark_allow_refill = self.manual_watermark_allow_refill

    def apply_watermark_unlocked(self) -> dict[str, Any]:
        if not self.config.watermark_mode or self.config.watermark_free_mb <= 0:
            return self.status_unlocked(extra={"allocated_bytes": 0, "released_bytes": 0, "watermark_action": "disabled"})

        free, _ = self.mem_info_unlocked()
        free_mb = free / MIB
        target_mb = self.config.watermark_free_mb
        hysteresis_mb = self.config.watermark_hysteresis_mb
        released = 0

        if free_mb < target_mb:
            need_bytes = int((target_mb - free_mb) * MIB)
            released = self.release_bytes_unlocked(need_bytes)
            if released > 0:
                self.watermark_cooldown_until = time.monotonic() + max(0.0, self.config.watermark_release_cooldown_sec)
            gc.collect()
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                LOG.debug("torch.cuda.ipc_collect failed", exc_info=True)
            status = self.status_unlocked(extra={"released_bytes": released, "allocated_bytes": 0, "watermark_action": "release"})
            if released > 0:
                LOG.info("watermark released %.2f MiB | %s", released / MIB, self.format_memory_summary(status))
            return status

        if free_mb > target_mb + hysteresis_mb:
            if not self.config.watermark_allow_refill:
                return self.status_unlocked(
                    extra={
                        "allocated_bytes": 0,
                        "released_bytes": 0,
                        "watermark_action": "no_refill",
                    }
                )
            cooldown_remaining = self.watermark_cooldown_until - time.monotonic()
            if cooldown_remaining > 0:
                return self.status_unlocked(
                    extra={
                        "allocated_bytes": 0,
                        "released_bytes": 0,
                        "watermark_action": "cooldown",
                        "watermark_cooldown_sec": round(cooldown_remaining, 2),
                    }
                )

            preserve_free_bytes = int((target_mb + hysteresis_mb) * MIB)
            allocated = self.fill_preserving_free_unlocked(preserve_free_bytes)
            status = self.status_unlocked(extra={"allocated_bytes": allocated})
            status["watermark_action"] = "fill"
            if allocated > 0:
                LOG.info("watermark filled %.2f MiB | %s", allocated / MIB, self.format_memory_summary(status))
            return status

        return self.status_unlocked(extra={"allocated_bytes": 0, "released_bytes": 0, "watermark_action": "hold"})

    def start_auto_refill(self) -> None:
        if self.auto_refill_thread is not None:
            return
        self.auto_refill_thread = threading.Thread(target=self._auto_refill_loop, name="vram-auto-refill", daemon=True)
        self.auto_refill_thread.start()
        LOG.info(
            "control loop enabled: auto_refill=%s interval=%ss min_delta=%s MiB watermark_interval=%ss",
            self.config.auto_refill,
            self.config.auto_refill_interval_sec,
            self.config.auto_refill_min_delta_mb,
            self.config.watermark_interval_sec,
        )

    def stop_auto_refill(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        if self.auto_refill_thread is not None:
            timeout = max(1.0, self.config.auto_refill_interval_sec, self.config.watermark_interval_sec)
            self.auto_refill_thread.join(timeout=timeout)

    def _auto_refill_loop(self) -> None:
        while True:
            interval = self.config.watermark_interval_sec if self.config.watermark_mode else self.config.auto_refill_interval_sec
            self.wake_event.wait(max(0.2, interval))
            self.wake_event.clear()
            if self.stop_event.is_set():
                break
            try:
                if not self.config.watermark_mode and not self.config.auto_refill:
                    continue
                status = self.maybe_refill()
                allocated = int(status.get("allocated_bytes", 0) or 0)
                if allocated > 0:
                    action = status.get("watermark_action")
                    prefix = "watermark" if action else "auto refill"
                    LOG.info("%s allocated %.2f MiB | %s", prefix, allocated / MIB, self.format_memory_summary(status))
            except Exception:
                LOG.exception("control loop failed")

    def set_fraction(self, fraction: float) -> dict[str, Any]:
        with self.lock:
            self.config.fraction = max(0.0, min(0.98, fraction))
            self.wake_event.set()
            return self.fill()

    def status_unlocked(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        free, total = self.mem_info_unlocked()
        held = self.held_bytes_unlocked()
        external_used = max(0, total - free - held)
        paused_sec = max(0.0, self.auto_refill_paused_until - time.monotonic())
        process_summary = self.gpu_process_summary_unlocked()
        target_used = self.target_used_bytes_unlocked()
        target = self.target_bytes_unlocked()
        data: dict[str, Any] = {
            "ok": True,
            "device": str(self.device),
            "fraction": self.config.fraction,
            "chunk_mb": self.config.chunk_mb,
            "min_free_mb": self.config.min_free_mb,
            "max_hold_mb": self.config.max_hold_mb,
            "auto_refill": self.config.auto_refill,
            "auto_refill_interval_sec": self.config.auto_refill_interval_sec,
            "auto_refill_min_delta_mb": self.config.auto_refill_min_delta_mb,
            "auto_refill_paused_sec": round(paused_sec, 2),
            "watermark_mode": self.config.watermark_mode,
            "watermark_free_mb": self.config.watermark_free_mb,
            "watermark_hysteresis_mb": self.config.watermark_hysteresis_mb,
            "watermark_allow_refill": self.config.watermark_allow_refill,
            "watermark_interval_sec": self.config.watermark_interval_sec,
            "watermark_release_cooldown_sec": self.config.watermark_release_cooldown_sec,
            "watermark_session_count": len(self.watermark_sessions),
            "chunks": len(self.chunks),
            "held_bytes": held,
            "held_mb": round(held / MIB, 2),
            "free_bytes": free,
            "free_mb": round(free / MIB, 2),
            "total_bytes": total,
            "total_mb": round(total / MIB, 2),
            "external_used_bytes": external_used,
            "external_used_mb": round(external_used / MIB, 2),
            "target_used_bytes": target_used,
            "target_used_mb": round(target_used / MIB, 2),
            "target_bytes": target,
            "target_mb": round(target / MIB, 2),
        }
        data.update(process_summary)
        if extra:
            data.update(extra)
        return data

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self.status_unlocked()

    def gpu_process_summary_unlocked(self) -> dict[str, Any]:
        processes = query_gpu_processes()
        if not processes:
            return {
                "gpu_process_query_ok": False,
                "guardian_process_used_mb": None,
                "comfyui_used_mb": None,
                "other_process_used_mb": None,
                "gpu_processes": [],
            }

        guardian_mb = 0.0
        comfyui_mb = 0.0
        other_mb = 0.0
        for process in processes:
            role = process["role"]
            used_mb = float(process["used_mb"])
            if role == "guardian":
                guardian_mb += used_mb
            elif role == "comfyui":
                comfyui_mb += used_mb
            else:
                other_mb += used_mb

        return {
            "gpu_process_query_ok": True,
            "guardian_process_used_mb": round(guardian_mb, 2),
            "comfyui_used_mb": round(comfyui_mb, 2),
            "other_process_used_mb": round(other_mb, 2),
            "gpu_processes": processes,
        }

    def format_memory_summary(self, status: dict[str, Any]) -> str:
        total = float(status.get("total_mb", 0) or 0)
        free = float(status.get("free_mb", 0) or 0)
        held = float(status.get("held_mb", 0) or 0)
        external = float(status.get("external_used_mb", 0) or 0)
        target = float(status.get("target_bytes", 0) or 0) / MIB
        paused = float(status.get("auto_refill_paused_sec", 0) or 0)

        if status.get("gpu_process_query_ok"):
            guardian_proc = float(status.get("guardian_process_used_mb", 0) or 0)
            comfyui = float(status.get("comfyui_used_mb", 0) or 0)
            other = float(status.get("other_process_used_mb", 0) or 0)
            process_bits = f"guardian_proc={guardian_proc:.0f}MiB comfyui={comfyui:.0f}MiB other={other:.0f}MiB"
        else:
            process_bits = "guardian_proc=unknown comfyui=unknown other=unknown"

        return (
            f"total={total:.0f}MiB free={free:.0f}MiB guardian_held={held:.0f}MiB "
            f"target={target:.0f}MiB external_calc={external:.0f}MiB {process_bits} paused={paused:.0f}s"
        )


class GuardianRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(65536).decode("utf-8", errors="replace").strip()
        try:
            request = parse_request(raw)
            response = self.server.guardian_command(request)  # type: ignore[attr-defined]
        except Exception as exc:
            LOG.exception("request failed: %r", raw)
            response = {"ok": False, "error": str(exc)}
        self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))


class GuardianTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], guardian: VramGuardian) -> None:
        super().__init__(address, GuardianRequestHandler)
        self.guardian = guardian

    def guardian_command(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = str(request.get("cmd", "")).lower()
        if cmd == "ping":
            return {"ok": True, "pong": True}
        if cmd == "status":
            return self.guardian.status()
        if cmd in {"fill", "reclaim", "reserve", "occupy", "hold", "allocate"}:
            mb = int(request.get("mb", 0) or 0)
            bytes_to_reserve = int(request.get("bytes", 0) or 0)
            if mb > 0:
                bytes_to_reserve = mb * MIB
            return self.guardian.reserve(bytes_to_reserve)
        if cmd in {"release", "release_all"}:
            mb = int(request.get("mb", 0) or 0)
            bytes_to_release = int(request.get("bytes", 0) or 0)
            pause_refill_sec = float(request.get("pause_refill_sec", 0) or 0)
            if mb > 0:
                bytes_to_release = mb * MIB
            return self.guardian.release(bytes_to_release, pause_refill_sec=pause_refill_sec)
        if cmd in {"ensure_free", "release_until_free"}:
            free_mb = int(request.get("free_mb", 0) or 0)
            pause_refill_sec = float(request.get("pause_refill_sec", 0) or 0)
            return self.guardian.ensure_free(free_mb, pause_refill_sec=pause_refill_sec)
        if cmd == "set_watermark":
            mode = parse_bool(request.get("mode"), True)
            free_mb = request.get("free_mb")
            hysteresis_mb = request.get("hysteresis_mb")
            allow_refill = request.get("allow_refill")
            token = request.get("token")
            return self.guardian.set_watermark(
                mode,
                free_mb=None if free_mb is None else int(free_mb),
                hysteresis_mb=None if hysteresis_mb is None else int(hysteresis_mb),
                allow_refill=None if allow_refill is None else parse_bool(allow_refill),
                token=None if token is None else str(token),
            )
        if cmd == "set_fraction":
            return self.guardian.set_fraction(float(request["fraction"]))
        raise ValueError(f"unknown command: {cmd!r}")


def parse_request(raw: str) -> dict[str, Any]:
    if not raw:
        raise ValueError("empty request")
    if raw.startswith("{"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON request must be an object")
        return data
    return {"cmd": raw}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return default if value is None or value == "" else float(value)


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return default if value is None or value == "" else int(value)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reserve CUDA VRAM and release or reclaim it on demand")
    parser.add_argument("--host", default=os.getenv("VRAM_GUARDIAN_HOST", "0.0.0.0"))
    parser.add_argument("--port", default=env_int("VRAM_GUARDIAN_PORT", 8765), type=int)
    parser.add_argument("--device", default=os.getenv("VRAM_GUARDIAN_DEVICE", "cuda:0"))
    parser.add_argument("--fraction", default=env_float("VRAM_GUARDIAN_FRACTION", 0.98), type=float)
    parser.add_argument("--min-free-mb", default=env_int("VRAM_GUARDIAN_MIN_FREE_MB", 0), type=int)
    parser.add_argument("--chunk-mb", default=env_int("VRAM_GUARDIAN_CHUNK_MB", 256), type=int)
    parser.add_argument("--max-hold-mb", default=env_int("VRAM_GUARDIAN_MAX_HOLD_MB", 0), type=int)
    parser.add_argument("--auto-refill", default=env_bool("VRAM_GUARDIAN_AUTO_REFILL", False), type=env_bool_arg)
    parser.add_argument(
        "--auto-refill-interval-sec",
        default=env_float("VRAM_GUARDIAN_AUTO_REFILL_INTERVAL_SEC", 5.0),
        type=float,
    )
    parser.add_argument(
        "--auto-refill-min-delta-mb",
        default=env_int("VRAM_GUARDIAN_AUTO_REFILL_MIN_DELTA_MB", 256),
        type=int,
    )
    parser.add_argument("--watermark-mode", default=env_bool("VRAM_GUARDIAN_WATERMARK_MODE", False), type=env_bool_arg)
    parser.add_argument("--watermark-free-mb", default=env_int("VRAM_GUARDIAN_WATERMARK_FREE_MB", 0), type=int)
    parser.add_argument("--watermark-hysteresis-mb", default=env_int("VRAM_GUARDIAN_WATERMARK_HYSTERESIS_MB", 2048), type=int)
    parser.add_argument(
        "--watermark-interval-sec",
        default=env_float("VRAM_GUARDIAN_WATERMARK_INTERVAL_SEC", 1.0),
        type=float,
    )
    parser.add_argument(
        "--watermark-release-cooldown-sec",
        default=env_float("VRAM_GUARDIAN_WATERMARK_RELEASE_COOLDOWN_SEC", 5.0),
        type=float,
    )
    parser.add_argument("--log-level", default=os.getenv("VRAM_GUARDIAN_LOG_LEVEL", "INFO"))
    return parser


def env_bool_arg(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return value.strip().lower() not in {"0", "false", "no", "off"}


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")

    guardian = VramGuardian(
        GuardianConfig(
            device=args.device,
            fraction=args.fraction,
            min_free_mb=args.min_free_mb,
            chunk_mb=args.chunk_mb,
            max_hold_mb=args.max_hold_mb,
            auto_refill=args.auto_refill,
            auto_refill_interval_sec=args.auto_refill_interval_sec,
            auto_refill_min_delta_mb=args.auto_refill_min_delta_mb,
            watermark_mode=args.watermark_mode,
            watermark_free_mb=args.watermark_free_mb,
            watermark_hysteresis_mb=args.watermark_hysteresis_mb,
            watermark_interval_sec=args.watermark_interval_sec,
            watermark_release_cooldown_sec=args.watermark_release_cooldown_sec,
        )
    )
    initial_status = guardian.fill()
    LOG.info("initial fill: %s", initial_status)
    LOG.info("memory summary: %s", guardian.format_memory_summary(initial_status))
    guardian.start_auto_refill()

    server = GuardianTCPServer((args.host, args.port), guardian)
    LOG.info("listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("stopping; releasing held VRAM")
        guardian.stop_auto_refill()
        guardian.release()
    finally:
        guardian.stop_auto_refill()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
