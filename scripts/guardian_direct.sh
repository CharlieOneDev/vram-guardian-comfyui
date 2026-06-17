#!/usr/bin/env bash
set -euo pipefail

COMMAND="${1:-status}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PYTHON_BIN="${PYTHON_BIN:-python}"
HOST="${VRAM_GUARDIAN_HOST:-0.0.0.0}"
PORT="${VRAM_GUARDIAN_PORT:-8765}"
DEVICE="${VRAM_GUARDIAN_DEVICE:-cuda:0}"
FRACTION="${VRAM_GUARDIAN_FRACTION:-0.98}"
MIN_FREE_MB="${VRAM_GUARDIAN_MIN_FREE_MB:-0}"
CHUNK_MB="${VRAM_GUARDIAN_CHUNK_MB:-256}"
MAX_HOLD_MB="${VRAM_GUARDIAN_MAX_HOLD_MB:-0}"
AUTO_REFILL="${VRAM_GUARDIAN_AUTO_REFILL:-false}"
AUTO_REFILL_INTERVAL_SEC="${VRAM_GUARDIAN_AUTO_REFILL_INTERVAL_SEC:-5}"
AUTO_REFILL_MIN_DELTA_MB="${VRAM_GUARDIAN_AUTO_REFILL_MIN_DELTA_MB:-256}"
WATERMARK_MODE="${VRAM_GUARDIAN_WATERMARK_MODE:-false}"
WATERMARK_FREE_MB="${VRAM_GUARDIAN_WATERMARK_FREE_MB:-0}"
WATERMARK_HYSTERESIS_MB="${VRAM_GUARDIAN_WATERMARK_HYSTERESIS_MB:-2048}"
WATERMARK_INTERVAL_SEC="${VRAM_GUARDIAN_WATERMARK_INTERVAL_SEC:-1}"
WATERMARK_RELEASE_COOLDOWN_SEC="${VRAM_GUARDIAN_WATERMARK_RELEASE_COOLDOWN_SEC:-5}"
LOG_FILE="${VRAM_GUARDIAN_LOG_FILE:-${ROOT_DIR}/vram_guardian.log}"
PID_FILE="${VRAM_GUARDIAN_PID_FILE:-${ROOT_DIR}/vram_guardian.pid}"

export PYTHONPATH="${ROOT_DIR}/guardian${PYTHONPATH:+:${PYTHONPATH}}"

client() {
  "${PYTHON_BIN}" -m vram_guardian.client "$@" --host 127.0.0.1 --port "${PORT}"
}

require_gb_amount() {
  if [ -z "${1:-}" ]; then
    echo "usage: $0 ${COMMAND} <GiB>" >&2
    exit 2
  fi
}

running_pid() {
  if [ -f "${PID_FILE}" ]; then
    local pid
    pid="$(cat "${PID_FILE}")"
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
      printf '%s\n' "${pid}"
      return 0
    fi
    rm -f "${PID_FILE}"
  fi
  return 1
}

preflight() {
  "${PYTHON_BIN}" -c 'import torch; print("torch", torch.__version__, "cuda", torch.cuda.is_available()); assert torch.cuda.is_available(), "CUDA is not available to this Python environment"'
}

case "${COMMAND}" in
  start)
    if pid="$(running_pid)"; then
      echo "VRAM Guardian is already running with PID ${pid}"
      exit 0
    fi

    preflight
    nohup "${PYTHON_BIN}" -m vram_guardian.server \
      --host "${HOST}" \
      --port "${PORT}" \
      --device "${DEVICE}" \
      --fraction "${FRACTION}" \
      --min-free-mb "${MIN_FREE_MB}" \
      --chunk-mb "${CHUNK_MB}" \
      --max-hold-mb "${MAX_HOLD_MB}" \
      --auto-refill "${AUTO_REFILL}" \
      --auto-refill-interval-sec "${AUTO_REFILL_INTERVAL_SEC}" \
      --auto-refill-min-delta-mb "${AUTO_REFILL_MIN_DELTA_MB}" \
      --watermark-mode "${WATERMARK_MODE}" \
      --watermark-free-mb "${WATERMARK_FREE_MB}" \
      --watermark-hysteresis-mb "${WATERMARK_HYSTERESIS_MB}" \
      --watermark-interval-sec "${WATERMARK_INTERVAL_SEC}" \
      --watermark-release-cooldown-sec "${WATERMARK_RELEASE_COOLDOWN_SEC}" \
      > "${LOG_FILE}" 2>&1 &
    echo "$!" > "${PID_FILE}"
    sleep 2

    if pid="$(running_pid)"; then
      echo "VRAM Guardian started with PID ${pid}"
      echo "Log: ${LOG_FILE}"
      client status || true
    else
      echo "VRAM Guardian failed to start. Last log lines:" >&2
      tail -n 80 "${LOG_FILE}" >&2 || true
      exit 1
    fi
    ;;

  status)
    if pid="$(running_pid)"; then
      echo "VRAM Guardian PID: ${pid}"
    else
      echo "VRAM Guardian is not running from ${PID_FILE}"
    fi
    client status
    ;;

  release|free)
    require_gb_amount "${2:-}"
    client release --gb "${2}"
    ;;

  release-all|free-all)
    client release_all
    ;;

  reserve|occupy|hold)
    require_gb_amount "${2:-}"
    client reserve --gb "${2}"
    ;;

  fill|reclaim)
    if [ -n "${2:-}" ]; then
      client "${COMMAND}" --gb "${2}"
    else
      client "${COMMAND}"
    fi
    ;;

  logs)
    tail -f "${LOG_FILE}"
    ;;

  stop)
    if pid="$(running_pid)"; then
      kill "${pid}" || true
      rm -f "${PID_FILE}"
      echo "Stopped VRAM Guardian PID ${pid}"
    else
      echo "VRAM Guardian is not running"
    fi
    ;;

  restart)
    "${BASH_SOURCE[0]}" stop
    "${BASH_SOURCE[0]}" start
    ;;

  *)
    echo "usage: $0 {start|status|release <GiB>|release-all|reserve <GiB>|fill|logs|stop|restart}" >&2
    exit 2
    ;;
esac
