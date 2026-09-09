#!/usr/bin/env python3
"""Backup DB live -> data/backups/, simpan N hari terakhir.

Memakai sqlite3.Connection.backup (API backup ONLINE) — aman pada DB yang sedang
ditulis engine, tidak seperti `cp` yang bisa menyalin file setengah transaksi.
Dijalankan cron 16:00 UTC (23:00 WIB), sesudah bursa tutup & laporan harian selesai.
"""
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # tools/ -> root repo
DB = ROOT / "data" / "prediction.db"
OUT = ROOT / "data" / "backups"
KEEP_DAYS = 7

def main() -> int:
    if not DB.exists():
        print(f"DB tidak ada: {DB}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"auto-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)          # konsisten walau engine sedang menulis
        finally:
            dst.close()
    finally:
        src.close()
    # verifikasi hasil bisa dibuka & punya tabel inti sebelum memangkas yang lama
    c = sqlite3.connect(dest)
    try:
        n = c.execute("SELECT count(*) FROM predictions").fetchone()[0]
    finally:
        c.close()
    print(f"backup OK: {dest.name} ({dest.stat().st_size/1e6:.1f} MB, {n} prediksi)")

    cutoff = time.time() - KEEP_DAYS * 86400
    for f in OUT.glob("auto-*.db"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            print(f"hapus lama: {f.name}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
