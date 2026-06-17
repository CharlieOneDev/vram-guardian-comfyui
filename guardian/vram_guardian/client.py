import argparse
import json
import socket
import sys


def request(host: str, port: int, payload: dict, timeout: float = 3.0) -> dict:
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(data)
        chunks = []
        while True:
            part = sock.recv(65536)
            if not part:
                break
            chunks.append(part)
            if b"\n" in part:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    return json.loads(raw.decode("utf-8"))


def requested_mb(mb: int, gb: float) -> int:
    if gb > 0:
        return int(gb * 1024)
    return max(0, mb)


def main() -> int:
    parser = argparse.ArgumentParser(description="VRAM Guardian control client")
    parser.add_argument(
        "cmd",
        choices=[
            "ping",
            "status",
            "release",
            "release_all",
            "reserve",
            "occupy",
            "hold",
            "allocate",
            "reclaim",
            "fill",
            "ensure_free",
            "set_watermark",
        ],
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--mb", default=0, type=int, help="amount in MiB; for release, 0 means all")
    parser.add_argument("--gb", default=0.0, type=float, help="amount in GiB")
    parser.add_argument("--pause-refill-sec", default=0.0, type=float, help="pause auto-refill after release")
    parser.add_argument("--mode", default=None, help="set_watermark mode: true/false")
    parser.add_argument("--free-mb", default=None, type=int, help="set_watermark target free VRAM")
    parser.add_argument("--hysteresis-mb", default=None, type=int, help="set_watermark hysteresis")
    parser.add_argument("--allow-refill", default=None, help="set_watermark refill behavior: true/false")
    parser.add_argument("--token", default=None, help="set_watermark session token")
    parser.add_argument("--timeout", default=3.0, type=float)
    args = parser.parse_args()

    payload = {"cmd": args.cmd}
    amount_mb = requested_mb(args.mb, args.gb)
    if args.cmd in {"release", "reserve", "occupy", "hold", "allocate", "reclaim", "fill"} and amount_mb > 0:
        payload["mb"] = amount_mb
    if args.cmd in {"release", "ensure_free"} and args.pause_refill_sec > 0:
        payload["pause_refill_sec"] = args.pause_refill_sec
    if args.cmd == "ensure_free":
        payload["free_mb"] = args.free_mb or amount_mb
    if args.cmd == "set_watermark":
        if args.mode is not None:
            payload["mode"] = args.mode
        if args.free_mb is not None:
            payload["free_mb"] = args.free_mb
        if args.hysteresis_mb is not None:
            payload["hysteresis_mb"] = args.hysteresis_mb
        if args.allow_refill is not None:
            payload["allow_refill"] = args.allow_refill
        if args.token is not None:
            payload["token"] = args.token

    print(json.dumps(request(args.host, args.port, payload, args.timeout), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
