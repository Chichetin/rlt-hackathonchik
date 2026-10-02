#!/usr/bin/env bash
# make data: the organizers' dataset from the GitHub release of this (private) repository.
#
#   bash scripts/data.sh              # processed parquet + raw CSV
#   bash scripts/data.sh processed    # only data/processed (enough for make demo / make eval)
#   bash scripts/data.sh raw          # only data/raw (needed to re-run src.ingest)
#   bash scripts/data.sh cache        # the snapshot of the source cache (names, roles, «Новые» of the demo lots);
#                                     # NOT part of `all`: it is a cache, not data. An existing local cache
#                                     # is merged (INSERT OR IGNORE by key: its newer rows stay); CACHE_FORCE=1 replaces it
#
# The data never goes to git (.gitignore, check-data-files.sh); the release assets are the storage.
# Every file is checked against SHA256SUMS of the release; a file that already matches is not
# downloaded again. A download goes to a temporary directory next to its target (same file system)
# and is renamed in only after the check, so an interrupted run leaves no truncated file.
set -euo pipefail

TAG="${DATA_TAG:-data-2026-10-02}"  # data-2026-10-01: ingest before the supplier profile (#435)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROCESSED="${DATA_PROCESSED_DIR:-$ROOT/data/processed}"
RAW="${DATA_RAW_DIR:-$ROOT/data/raw}"
PART="${1:-all}"
RAW_ZIP="raw-24-25.zip"
CACHE_ASSET="sources-cache.sqlite"  # public answers of the registries (url, body, status, time): no keys, no headers
CACHE_SUMS="SHA256SUMS.cache"       # its own checksum list: the SHA256SUMS of the data stay untouched
CACHE_TARGET="${SOURCE_CACHE:-$ROOT/cache/sources.sqlite}"
PROCESSED_FILES=(notices.parquet participants.parquet items.parquet suppliers.parquet
                 suppliers_2025-07-01.parquet manifest.json)

fail() { echo "make data: $*" >&2; exit 1; }

case "$PART" in all|processed|raw|cache) ;; *) fail "неизвестная часть '$PART' (all | processed | raw | cache)";; esac
command -v gh >/dev/null 2>&1 || fail "нужен GitHub CLI (gh) с доступом к репозиторию: https://cli.github.com, затем gh auth login"
cd "$ROOT"  # gh takes the repository from the current directory

sha() {  # sha256 of a file: Linux and Git Bash have sha256sum, macOS has shasum
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

TMP_DIRS=()
cleanup() { for d in "${TMP_DIRS[@]:-}"; do if [ -n "$d" ]; then rm -rf "$d"; fi; done; }
trap cleanup EXIT

tmp_in() {  # NEW_TMP = a temporary directory inside <dir>: the final rename stays on one file system
  mkdir -p "$1"  # (a variable, not stdout: a subshell would lose the directory for the cleanup)
  NEW_TMP="$(mktemp -d "$1/.download.XXXXXX")"
  TMP_DIRS+=("$NEW_TMP")
}

download() {  # download <asset> <dir>; gh prints signed asset URLs on network errors — the query is cut out
  local err
  if ! err="$(gh release download "$TAG" -p "$1" -D "$2" --clobber 2>&1)"; then
    echo "$err" | sed -E 's/\?[^" ]*//g' >&2
    return 1
  fi
}

SUMS_DIR="$(mktemp -d)"  # the checksum list is tiny: the system temporary directory is fine
TMP_DIRS+=("$SUMS_DIR")
SUMS_ASSET=SHA256SUMS
[ "$PART" = cache ] && SUMS_ASSET="$CACHE_SUMS"
download "$SUMS_ASSET" "$SUMS_DIR" \
  || fail "релиз $TAG недоступен или в нём нет $SUMS_ASSET (сеть, gh auth login или нет доступа к репозиторию)"

expected() { awk -v f="$1" '$2 == f { print $1 }' "$SUMS_DIR/$SUMS_ASSET"; }

fetch() {  # fetch <asset> <target dir>: skip a matching file, otherwise download, verify, rename in
  local name="$1" dir="$2" want tmp
  want="$(expected "$name")"
  [ -n "$want" ] || fail "в SHA256SUMS релиза $TAG нет файла $name"
  if [ -f "$dir/$name" ] && [ "$(sha "$dir/$name")" = "$want" ]; then
    echo "ok        $name (уже на месте)"
    return 0
  fi
  echo "download  $name"
  tmp_in "$dir"
  tmp="$NEW_TMP"
  download "$name" "$tmp" || fail "не скачался $name"
  [ "$(sha "$tmp/$name")" = "$want" ] || fail "контрольная сумма $name не сошлась — файл не записан"
  mv -f "$tmp/$name" "$dir/$name"
}

csv_sums() {  # "<sha256>  <name>" of every CSV in <dir>, sorted by name
  local f
  for f in "$1"/*.csv; do [ -f "$f" ] && echo "$(sha "$f")  $(basename "$f")"; done | LC_ALL=C sort -k2
}

if [ "$PART" = all ] || [ "$PART" = processed ]; then
  for f in "${PROCESSED_FILES[@]}"; do fetch "$f" "$PROCESSED"; done  # manifest.json last: demo.sh keys on it
  echo "data/processed: готово ($PROCESSED)"
fi

if [ "$PART" = all ] || [ "$PART" = raw ]; then
  want="$(expected "$RAW_ZIP")"
  [ -n "$want" ] || fail "в SHA256SUMS релиза $TAG нет файла $RAW_ZIP"
  # The release has no checksum of the CSVs themselves: the marker keeps the zip checksum and the checksum of
  # every unpacked CSV, so a damaged, missing or foreign CSV is unpacked again.
  marker="$RAW/.release-$TAG.sha256"
  if [ -f "$marker" ] && [ "$(head -n 1 "$marker")" = "$want" ] && [ "$(tail -n +2 "$marker")" = "$(csv_sums "$RAW")" ] \
     && [ "$(tail -n +2 "$marker" | wc -l | tr -d ' ')" -ge 3 ]; then
    echo "ok        $RAW_ZIP (уже распакован)"
  else
    echo "download  $RAW_ZIP"
    tmp_in "$RAW"
    tmp="$NEW_TMP"
    download "$RAW_ZIP" "$tmp" || fail "не скачался $RAW_ZIP"
    [ "$(sha "$tmp/$RAW_ZIP")" = "$want" ] || fail "контрольная сумма $RAW_ZIP не сошлась — архив не распакован"
    mkdir -p "$tmp/unpacked"
    # Python unpacks the Cyrillic file names the same way on Linux, macOS and Git Bash; uv brings its own Python
    if command -v uv >/dev/null 2>&1; then unzip_cmd=(uv run --frozen python); else unzip_cmd=(python3); fi
    PYTHONUTF8=1 "${unzip_cmd[@]}" -m zipfile -e "$tmp/$RAW_ZIP" "$tmp/unpacked" || fail "не распаковался $RAW_ZIP"
    rm -f "$marker"
    find "$tmp/unpacked" -name '*.csv' -exec mv -f {} "$RAW/" \;
    { echo "$want"; csv_sums "$RAW"; } >"$marker"
  fi
  echo "data/raw: готово ($RAW)"
fi

if [ "$PART" = cache ]; then
  want="$(expected "$CACHE_ASSET")"
  [ -n "$want" ] || fail "в $CACHE_SUMS релиза $TAG нет файла $CACHE_ASSET"
  dir="$(dirname "$CACHE_TARGET")"
  if [ -f "$CACHE_TARGET" ] && [ "$(sha "$CACHE_TARGET")" = "$want" ]; then
    echo "ok        $CACHE_ASSET (уже на месте)"
  else
    echo "download  $CACHE_ASSET"
    tmp_in "$dir"
    tmp="$NEW_TMP"
    download "$CACHE_ASSET" "$tmp" || fail "не скачался $CACHE_ASSET"
    [ "$(sha "$tmp/$CACHE_ASSET")" = "$want" ] || fail "контрольная сумма $CACHE_ASSET не сошлась — файл не записан"
    if [ -f "$CACHE_TARGET" ] && [ "${CACHE_FORCE:-}" != 1 ]; then
      # the local cache (even an empty one that `make demo` creates on the first search) keeps its newer rows:
      # the snapshot only adds the keys it lacks
      if command -v uv >/dev/null 2>&1; then py=(uv run --frozen python); else py=(python3); fi
      "${py[@]}" -c '
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("CREATE TABLE IF NOT EXISTS pages (key TEXT PRIMARY KEY, url TEXT NOT NULL, "
            "body TEXT NOT NULL, status INTEGER NOT NULL, fetched_at REAL NOT NULL)")
con.execute("ATTACH DATABASE ? AS snap", (sys.argv[2],))
before = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
con.execute("INSERT OR IGNORE INTO pages (key, url, body, status, fetched_at) "
            "SELECT key, url, body, status, fetched_at FROM snap.pages")
con.commit()
print("cache: добавлено из снимка записей:", con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] - before)
' "$CACHE_TARGET" "$tmp/$CACHE_ASSET" || fail "снимок не влит в $CACHE_TARGET (CACHE_FORCE=1 заменит файл целиком)"
    else
      mv -f "$tmp/$CACHE_ASSET" "$CACHE_TARGET"
    fi
  fi
  echo "cache: готово ($CACHE_TARGET)"
fi
