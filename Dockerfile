ARG BASE_IMAGE=pytorch/pytorch:2.7.1-cuda12.6-cudnn9-runtime
FROM ${BASE_IMAGE}

WORKDIR /app
COPY guardian/ /app/

ENV PYTHONUNBUFFERED=1
ENV VRAM_GUARDIAN_HOST=0.0.0.0
ENV VRAM_GUARDIAN_PORT=8765

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python -m vram_guardian.client status --host 127.0.0.1 --port ${VRAM_GUARDIAN_PORT} >/dev/null || exit 1

ENTRYPOINT ["python", "-m", "vram_guardian.server"]
