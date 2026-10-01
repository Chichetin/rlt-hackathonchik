#!/usr/bin/env bash
# make data: the organizers' dataset from the GitHub release of this (private) repository.
#
#   bash scripts/data.sh              # processed parquet + raw CSV
#   bash scripts/data.sh processed    # only data/processed (enough for make demo / make eval)
#   bash scripts/data.sh raw          # only data/raw (needed to re-run src.ingest)
#
# The data never goes to git (.gitignore, check-data-files.sh); the release assets are the storage.
# Every file is checked against SHA256SUMS of the release; a file that already matches is not
# downloaded again, a download goes to a temporary directory and is moved in only after the check.
set -euo pipefail

TAG="${DATA_TAG:-data-2026-10-01}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROCESSED="${DATA_PROCESSED_DIR:-$ROOT/data/processed}"
RAW="${DATA_RAW_DIR:-$ROOT/data/raw}"
PART="${1:-all}"
RAW_ZIP="raw-24-25.zip"
PROCESSED_FILES=(notices.parquet participants.parquet items.parquet suppliers.parquet
                 suppliers_2025-07-01.parquet manifest.json)

fail() { echo "make data: $*" >&2; exit 1; }

case "$PART" in all|processed|raw) ;; *) fail "неизвестная часть '$PART' (all | processed | raw)";; esac
command -v gh >/dev/null 2>&1 || fail "нужен GitHub CLI (gh) с доступом к репозиторию: https://cli.github.com, затем gh auth login"

sha() {  # sha256 of a file: Linux and Git Bash have sha256sum, macOS has shasum
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

gh release download "$TAG" -p SHA256SUMS -D "$TMP" --clobber 2>"$TMP/err" \
  || fail "релиз $TAG недоступен (сеть, gh auth login или нет доступа к репозиторию): $(tr '\n' ' ' <"$TMP/err")"

expected() { awk -v f="$1" '$2 == f { print $1 }' "$TMP/SHA256SUMS"; }

fetch() {  # fetch <asset> <target dir>: skip a matching file, otherwise download, verify, move in
  local name="$1" dir="$2" want
  want="$(expected "$name")"
  [ -n "$want" ] || fail "в SHA256SUMS релиза $TAG нет файла $name"
  if [ -f "$dir/$name" ] && [ "$(sha "$dir/$name")" = "$want" ]; then
    echo "ok        $name (уже на месте)"
    return 0
  fi
  echo "download  $name"
  gh release download "$TAG" -p "$name" -D "$TMP" --clobber || fail "не скачался $name"
  [ "$(sha "$TMP/$name")" = "$want" ] || fail "контрольная сумма $name не сошлась — файл не записан"
  mkdir -p "$dir"
  mv -f "$TMP/$name" "$dir/$name"
}

if [ "$PART" = all ] || [ "$PART" = processed ]; then
  for f in "${PROCESSED_FILES[@]}"; do fetch "$f" "$PROCESSED"; done
  echo "data/processed: готово ($PROCESSED)"
fi

if [ "$PART" = all ] || [ "$PART" = raw ]; then
  want="$(expected "$RAW_ZIP")"
  [ -n "$want" ] || fail "в SHA256SUMS релиза $TAG нет файла $RAW_ZIP"
  marker="$RAW/.release-$TAG.sha256"  # the CSVs themselves have no checksum in the release: the marker names the zip
  csv_count=0
  if [ -d "$RAW" ]; then csv_count="$(find "$RAW" -maxdepth 1 -name '*.csv' | wc -l | tr -d ' ')"; fi
  if [ -f "$marker" ] && [ "$(cat "$marker")" = "$want" ] && [ "$csv_count" -ge 3 ]; then
    echo "ok        $RAW_ZIP (уже распакован)"
  else
    echo "download  $RAW_ZIP"
    gh release download "$TAG" -p "$RAW_ZIP" -D "$TMP" --clobber || fail "не скачался $RAW_ZIP"
    [ "$(sha "$TMP/$RAW_ZIP")" = "$want" ] || fail "контрольная сумма $RAW_ZIP не сошлась — архив не распакован"
    mkdir -p "$TMP/raw" "$RAW"
    # Python unpacks the Cyrillic file names the same way on Linux, macOS and Git Bash
    PYTHONUTF8=1 python3 -m zipfile -e "$TMP/$RAW_ZIP" "$TMP/raw" 2>/dev/null \
      || PYTHONUTF8=1 python -m zipfile -e "$TMP/$RAW_ZIP" "$TMP/raw" || fail "не распаковался $RAW_ZIP"
    find "$TMP/raw" -name '*.csv' -exec mv -f {} "$RAW/" \;
    echo "$want" >"$marker"
  fi
  echo "data/raw: готово ($RAW)"
fi
