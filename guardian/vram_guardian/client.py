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


def main() -> int:
    parser = argparse.ArgumentParser(description="VRAM Guardian control client")
    parser.add_argument("cmd", choices=["ping", "status", "release", "reclaim", "fill"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--mb", default=0, type=int, help="amount to release, 0 means all")
    parser.add_argument("--pause-refill-sec", default=0.0, type=float, help="pause auto-refill after release")
    parser.add_argument("--timeout", default=3.0, type=float)
    args = parser.parse_args()

    payload = {"cmd": args.cmd}
    if args.cmd == "release" and args.mb > 0:
        payload["mb"] = args.mb
    if args.cmd == "release" and args.pause_refill_sec > 0:
        payload["pause_refill_sec"] = args.pause_refill_sec

    print(json.dumps(request(args.host, args.port, payload, args.timeout), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
