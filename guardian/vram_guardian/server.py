import argparse
import gc
import json
import logging
import os
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import torch


MIB = 1024 * 1024
LOG = logging.getLogger("vram_guardian")


@dataclass
class GuardianConfig:
    device: str = "cuda:0"
    fraction: float = 0.82
    min_free_mb: int = 1536
    chunk_mb: int = 256
    max_hold_mb: int = 0
    auto_refill: bool = True
    auto_refill_interval_sec: float = 5.0
    auto_refill_min_delta_mb: int = 256

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
        self.auto_refill_thread: threading.Thread | None = None
        self.auto_refill_paused_until = 0.0

    def held_bytes_unlocked(self) -> int:
        return sum(chunk.numel() * chunk.element_size() for chunk in self.chunks)

    def mem_info_unlocked(self) -> tuple[int, int]:
        free, total = torch.cuda.mem_get_info(self.device)
        return int(free), int(total)

    def target_bytes_unlocked(self) -> int:
        _, total = self.mem_info_unlocked()
        target = int(total * max(0.0, min(0.98, self.config.fraction)))
        target = min(target, max(0, total - self.config.min_free_bytes))
        if self.config.max_hold_bytes > 0:
            target = min(target, self.config.max_hold_bytes)
        return max(0, target)

    def fill(self, clear_pause: bool = True) -> dict[str, Any]:
        with self.lock:
            if clear_pause:
                self.auto_refill_paused_until = 0.0
            allocated = 0
            attempts = 0
            while True:
                held = self.held_bytes_unlocked()
                free, _ = self.mem_info_unlocked()
                target = self.target_bytes_unlocked()
                remaining_to_target = target - held
                available_to_take = free - self.config.min_free_bytes

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

            return self.status_unlocked(extra={"allocated_bytes": allocated})

    def pause_auto_refill(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self.lock:
            self.auto_refill_paused_until = max(self.auto_refill_paused_until, time.monotonic() + seconds)

    def maybe_refill(self) -> dict[str, Any]:
        with self.lock:
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
            released = 0
            if bytes_to_release <= 0:
                released = self.held_bytes_unlocked()
                self.chunks.clear()
            else:
                while self.chunks and released < bytes_to_release:
                    chunk = self.chunks.pop()
                    released += chunk.numel() * chunk.element_size()
                    del chunk

            gc.collect()
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                LOG.debug("torch.cuda.ipc_collect failed", exc_info=True)

            return self.status_unlocked(extra={"released_bytes": released})

    def start_auto_refill(self) -> None:
        if not self.config.auto_refill or self.auto_refill_thread is not None:
            return
        self.auto_refill_thread = threading.Thread(target=self._auto_refill_loop, name="vram-auto-refill", daemon=True)
        self.auto_refill_thread.start()
        LOG.info(
            "auto refill enabled: interval=%ss min_delta=%s MiB",
            self.config.auto_refill_interval_sec,
            self.config.auto_refill_min_delta_mb,
        )

    def stop_auto_refill(self) -> None:
        self.stop_event.set()
        if self.auto_refill_thread is not None:
            self.auto_refill_thread.join(timeout=max(1.0, self.config.auto_refill_interval_sec))

    def _auto_refill_loop(self) -> None:
        while not self.stop_event.wait(max(0.5, self.config.auto_refill_interval_sec)):
            try:
                status = self.maybe_refill()
                allocated = int(status.get("allocated_bytes", 0) or 0)
                if allocated > 0:
                    LOG.info("auto refill allocated %.2f MiB", allocated / MIB)
            except Exception:
                LOG.exception("auto refill failed")

    def set_fraction(self, fraction: float) -> dict[str, Any]:
        with self.lock:
            self.config.fraction = max(0.0, min(0.98, fraction))
            return self.fill()

    def status_unlocked(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        free, total = self.mem_info_unlocked()
        held = self.held_bytes_unlocked()
        external_used = max(0, total - free - held)
        paused_sec = max(0.0, self.auto_refill_paused_until - time.monotonic())
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
            "chunks": len(self.chunks),
            "held_bytes": held,
            "held_mb": round(held / MIB, 2),
            "free_bytes": free,
            "free_mb": round(free / MIB, 2),
            "total_bytes": total,
            "total_mb": round(total / MIB, 2),
            "external_used_bytes": external_used,
            "external_used_mb": round(external_used / MIB, 2),
            "target_bytes": self.target_bytes_unlocked(),
        }
        if extra:
            data.update(extra)
        return data

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self.status_unlocked()


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
        if cmd in {"fill", "reclaim"}:
            return self.guardian.fill()
        if cmd in {"release", "release_all"}:
            mb = int(request.get("mb", 0) or 0)
            bytes_to_release = int(request.get("bytes", 0) or 0)
            pause_refill_sec = float(request.get("pause_refill_sec", 0) or 0)
            if mb > 0:
                bytes_to_release = mb * MIB
            return self.guardian.release(bytes_to_release, pause_refill_sec=pause_refill_sec)
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
    parser = argparse.ArgumentParser(description="Reserve CUDA VRAM and release it on demand")
    parser.add_argument("--host", default=os.getenv("VRAM_GUARDIAN_HOST", "0.0.0.0"))
    parser.add_argument("--port", default=env_int("VRAM_GUARDIAN_PORT", 8765), type=int)
    parser.add_argument("--device", default=os.getenv("VRAM_GUARDIAN_DEVICE", "cuda:0"))
    parser.add_argument("--fraction", default=env_float("VRAM_GUARDIAN_FRACTION", 0.82), type=float)
    parser.add_argument("--min-free-mb", default=env_int("VRAM_GUARDIAN_MIN_FREE_MB", 1536), type=int)
    parser.add_argument("--chunk-mb", default=env_int("VRAM_GUARDIAN_CHUNK_MB", 256), type=int)
    parser.add_argument("--max-hold-mb", default=env_int("VRAM_GUARDIAN_MAX_HOLD_MB", 0), type=int)
    parser.add_argument("--auto-refill", default=env_bool("VRAM_GUARDIAN_AUTO_REFILL", True), type=env_bool_arg)
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
        )
    )
    LOG.info("initial fill: %s", guardian.fill())
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
