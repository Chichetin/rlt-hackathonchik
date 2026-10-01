#!/usr/bin/env bash
# make seal-check: holdout читают только src/eval/split.py и final.py (D-010).
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 2
hits=$(grep -rnE "unseal\(|holdout\.jsonl|load_cases\([\"']holdout" src --include='*.py' | grep -vE '^src/eval/(split|final)\.py:')
if [ -n "$hits" ]; then echo "seal: holdout вне src/eval/split.py и final.py:"; echo "$hits"; exit 1; fi
echo "seal: holdout читается только через make eval-final"
