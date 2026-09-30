#!/usr/bin/env bash
# Скачивает кандидатов эмбеддинг-моделей для 20-минутного бенчмарка (research/METHODOLOGY.md,
# источник — раздел 5 docs/source/hackathon-research-workflow.md) в кеш Hugging Face
# (~/.cache/huggingface/hub). Wi-Fi площадки — частая точка отказа: качать до 01.10.
# Повторный запуск докачивает только недостающее. Torch не нужен.
#   bash scripts/download-models.sh            # все
#   bash scripts/download-models.sh intfloat/multilingual-e5-small
set -uo pipefail

MODELS=(
  intfloat/multilingual-e5-small   # e5: префиксы "query: " и "passage: " обязательны
  intfloat/multilingual-e5-base
  intfloat/multilingual-e5-large
  BAAI/bge-m3:bin                  # веса только в pytorch_model.bin
  deepvk/USER-bge-m3
  ai-forever/FRIDA
)
[ $# -gt 0 ] && MODELS=("$@")

fail=0
for entry in "${MODELS[@]}"; do
  m="${entry%%:*}"
  echo "== $m"
  # Только safetensors и конфиги: без дублей в pytorch_model.bin/onnx/openvino.
  # Суффикс :bin — у модели нет safetensors, веса берём из *.bin.
  excl=(--exclude '*onnx*' --exclude '*openvino*' --exclude '*.ot' --exclude '*.msgpack' --exclude '*.h5' --exclude '*.gguf')
  [ "${entry#*:}" = bin ] || excl+=(--exclude '*.bin')
  if ! uvx --from 'huggingface_hub[cli]' hf download "$m" "${excl[@]}"; then
    echo "!! $m не скачана"; fail=1
  fi
done
exit $fail
