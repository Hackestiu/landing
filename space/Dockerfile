# Cultura Viva demo — the UNO Q pipeline as a self-hosted container.
#
# Python 3.12: onnxruntime requires >= 3.11 and every other wheel here is
# published for 3.12. Moving off it means re-checking the whole set.

# ---------------------------------------------------------------------------
# Stage 1 — compile llama-cpp-python.
#
# It has no usable prebuilt wheel for a glibc image: PyPI ships source only,
# and the project's CPU wheel index is musl-linked despite the linux_x86_64
# tag, so its libllama.so fails to load on Debian. Compiling here keeps the
# toolchain out of the runtime image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS llama-builder

ARG LLAMA_CPP_VERSION=0.3.19
# Each parallel compile job costs memory. 2 matches the CX22's 2 vCPU; raise
# it on a bigger box to build faster.
ARG BUILD_JOBS=2

# GGML_NATIVE=OFF: without it cmake targets the build machine's exact CPU, and
# the image then crashes with SIGILL on a host of a different CPU generation.
ENV CMAKE_ARGS="-DGGML_NATIVE=OFF" \
    CMAKE_BUILD_PARALLEL_LEVEL=${BUILD_JOBS}

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

RUN pip wheel --no-deps --no-cache-dir --wheel-dir /wheels \
    "llama-cpp-python==${LLAMA_CPP_VERSION}"

# ---------------------------------------------------------------------------
# Stage 2 — runtime.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Keep the Hub cache inside the models volume, so a container rebuild reuses
    # the downloaded weights instead of pulling ~700 MB again.
    HF_HOME=/app/models/.hf-cache \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7860

# ffmpeg: faster-whisper decodes whatever container the browser recorded.
# espeak-ng: Piper's phonemiser. libgomp1: OpenMP for onnxruntime/ctranslate2.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg espeak-ng libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app
WORKDIR /app

# Install the compiled wheel first so the requirements install finds
# llama-cpp-python already satisfied and does not try to build it again.
COPY --from=llama-builder /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# models/ ships the knowledge sheets and the vision labels, but it is also where
# the weights land at runtime — and it is a mounted volume in production, which
# would hide anything baked in. Stash the baked files aside; entrypoint.sh seeds
# the volume from there on first start.
RUN mv /app/models /opt/seed-models \
    && mkdir -p /app/models /app/data \
    && sed -i 's/\r$//' /app/entrypoint.sh \
    && chown -R app:app /app /opt/seed-models \
    && chmod +x /app/entrypoint.sh

USER app
EXPOSE 7860

# Model download plus preload takes minutes on a cold volume; the healthcheck
# must not start failing the container before that finishes.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20m --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:7860/').read(1)"

ENTRYPOINT ["/app/entrypoint.sh"]
