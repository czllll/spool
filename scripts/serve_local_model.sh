#!/usr/bin/env bash
# Serve a local model with an OpenAI-compatible API for Spool (`--model local`).
#
#   scripts/serve_local_model.sh            # Qwen3.5-4B Q4_K_M on :8089, 3 parallel slots
#   MODEL=path/to/model.gguf PARALLEL=1 scripts/serve_local_model.sh
#
# Needs llama.cpp (`brew install llama.cpp`). The model is downloaded on first use.
set -euo pipefail
cd "$(dirname "$0")/.."

MODEL="${MODEL:-.models/Qwen3.5-4B-Q4_K_M.gguf}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/main/Qwen3.5-4B-Q4_K_M.gguf}"
PORT="${PORT:-8089}"
PARALLEL="${PARALLEL:-3}"
CTX_PER_SLOT="${CTX_PER_SLOT:-32768}"

if [[ ! -f "$MODEL" ]]; then
  mkdir -p "$(dirname "$MODEL")"
  echo "downloading $MODEL_URL"
  curl -L --fail --retry 3 -o "$MODEL.part" "$MODEL_URL"
  mv "$MODEL.part" "$MODEL"
fi

# --jinja: use the model's chat template, which carries the tool-calling format.
# -c is the total context, split across --parallel slots.
# Prompt caching is on by default; each slot keeps the KV cache of its last request,
# so an append-only conversation only pays for the new tokens.
exec llama-server -m "$MODEL" --host 127.0.0.1 --port "$PORT" --jinja \
  --parallel "$PARALLEL" -c $((CTX_PER_SLOT * PARALLEL)) --metrics
