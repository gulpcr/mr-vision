# ============================================================
# Stage 1: CPU base (API server, lightweight tasks)
# ============================================================
FROM python:3.11-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgl1 \
    libglx-mesa0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -e ".[dev]"

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# --- API server target ---
FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]

# ============================================================
# Stage 2: GPU worker (CUDA-enabled for inference)
# ============================================================
FROM nvidia/cuda:12.6.3-runtime-ubuntu22.04 AS gpu-base

ENV DEBIAN_FRONTEND=noninteractive

# Step 1: Add deadsnakes PPA for Python 3.11
RUN apt-get update && apt-get install -y --no-install-recommends \
    software-properties-common \
    curl \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3.11-distutils \
    build-essential \
    libpq-dev \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgl1-mesa-glx \
    && rm -rf /var/lib/apt/lists/*

# Make python3.11 the default and install pip
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 && \
    update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 && \
    curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11

WORKDIR /app

COPY pyproject.toml .

# Install PyTorch with CUDA 12.8 support (Blackwell sm_120 compatible)
# torchvision is required by SpineNet (external/spinenet); pinned to the
# torch 2.7.0-matching release and pulled from the same CUDA wheel index.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu128 && \
    pip install --no-cache-dir -e ".[dev]"

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility

# --- GPU worker target ---
FROM gpu-base AS worker
CMD ["celery", "-A", "app.infrastructure.queue.celery_app:celery_app", "worker", \
     "--loglevel=info", "--concurrency=2", "-Q", "mri_inference"]
