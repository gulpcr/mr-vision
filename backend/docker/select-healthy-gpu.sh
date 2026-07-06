#!/bin/bash
# Runs before the Celery worker starts. On this host's Windows/WDDM GPUs, a
# wedged card can still answer nvidia-smi queries cleanly while failing real
# compute (the failure mode that took GPU 0 offline previously and required a
# host reboot + a hand-pinned UUID override to work around). So instead of
# trusting nvidia-smi, each visible GPU gets a real matmul under a hard
# timeout, and CUDA_VISIBLE_DEVICES is narrowed to whichever pass. Falls back
# to CPU if none do, rather than failing the container outright.
set -uo pipefail

# .env sets a static CUDA_VISIBLE_DEVICES for tooling run outside Docker; if
# left in place here it would restrict every probe below to whatever single
# device it names before we've even measured anything. Clear it so each
# cuda:${idx} probe addresses the real physical GPU at that index.
unset CUDA_VISIBLE_DEVICES

mapfile -t indices < <(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null)
healthy=()

for idx in "${indices[@]}"; do
  ok=0
  # Retry: right after a worker restart, a GPU the previous container held a
  # CUDA context on can transiently fail while that context is torn down —
  # not a real hardware fault. Three tries with a short gap avoids excluding
  # a perfectly good GPU because of that handoff window.
  for attempt in 1 2 3; do
    if timeout -k 5 20s python3 -c "
import torch
x = torch.randn(2000, 2000, device='cuda:${idx}')
(x @ x).sum().item()
" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 2
  done
  if [ "$ok" -eq 1 ]; then
    healthy+=("$idx")
    echo "[select-healthy-gpu] GPU ${idx} passed compute health check" >&2
  else
    echo "[select-healthy-gpu] GPU ${idx} FAILED compute health check after 3 attempts — excluding" >&2
  fi
done

if [ ${#healthy[@]} -eq 0 ]; then
  echo "[select-healthy-gpu] No healthy GPU found — falling back to CPU" >&2
  export CUDA_VISIBLE_DEVICES=""
else
  export CUDA_VISIBLE_DEVICES=$(IFS=,; echo "${healthy[*]}")
  echo "[select-healthy-gpu] Using GPU(s): ${CUDA_VISIBLE_DEVICES}" >&2
fi

exec "$@"
