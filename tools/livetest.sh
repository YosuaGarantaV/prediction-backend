#!/usr/bin/env bash
# Jalankan test suite DI VM tanpa mengotori DB produksi.
#
# Kenapa ada: `pytest` di live menulis ke DB live. Terbukti 9 Sep 2026 — 99 baris
# `agent_logs` dari provider palsu (_fake/_f1/_f2/_f3/_fa/_fb) masuk ke papan log
# produksi dan harus dihapus manual. Skrip ini menyalin DB ke DATA_DIR terpisah dan
# menautkan file data lain (universe, arsip arus asing, dsb) supaya test tetap melihat
# data nyata tapi menulis ke salinan.
#
#   bash tools/livetest.sh            # seluruh suite
#   bash tools/livetest.sh test_x.py  # satu file
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH=/tmp/testdb

rm -rf "$SCRATCH" && mkdir -p "$SCRATCH"
cd "$REPO/data"
for f in *; do
  case "$f" in prediction.db|prediction.db-*) continue;; esac
  ln -s "$REPO/data/$f" "$SCRATCH/$f"
done
# .backup, bukan cp: DB live memakai WAL dan sedang ditulis engine.
python3 - "$REPO/data/prediction.db" "$SCRATCH/prediction.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src); d = sqlite3.connect(dst)
s.backup(d); d.close(); s.close()
PY

cd "$REPO"
DATA_DIR="$SCRATCH" .venv/bin/python -m pytest -q "$@"
