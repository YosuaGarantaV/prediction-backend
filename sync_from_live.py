"""Mirror: tarik snapshot DB dari server LIVE lalu simpan ke DB lokal (cadangan).

Login ke server live (AUTH_USER/AUTH_PASSWORD) -> GET /api/backup (snapshot SQLite konsisten)
-> tulis atomik ke config.DB_PATH. Tidak menjalankan LLM apa pun. Dipakai run.py saat start
mode mirror, atau berdiri sendiri:  python sync_from_live.py
"""
from __future__ import annotations

import os
import shutil
import time

import requests

import config

# Backup BERPUTAR: sebelum DB lokal ditimpa snapshot live, salin dulu ke
# data/backups/prediction-YYYYmmdd-HHMMSS.db (simpan BACKUP_KEEP terakhir) — jaring
# pengaman rollback bila tarikan buruk / data hilang. Pulihkan: python restore_db.py
BACKUP_DIR = config.DATA_DIR / "backups"
BACKUP_KEEP = 5


def _rotate_backup(dest: str) -> None:
    """Salin DB lama ke backup bertimestamp, pangkas yang tertua (> BACKUP_KEEP)."""
    if not os.path.exists(dest):
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(dest, BACKUP_DIR / f"prediction-{stamp}.db")
    old = sorted(BACKUP_DIR.glob("prediction-*.db"))[:-BACKUP_KEEP]
    for f in old:
        try:
            f.unlink()
        except OSError:
            pass


def pull(dest: str | None = None) -> int:
    """Tarik DB live -> dest (default config.DB_PATH). Return jumlah byte tersimpan.
    Raise kalau MIRROR_UPSTREAM kosong, login gagal, atau unduhan gagal."""
    up = config.MIRROR_UPSTREAM
    if not up:
        raise RuntimeError("MIRROR_UPSTREAM belum di-set di .env")
    dest = str(dest or config.DB_PATH)

    s = requests.Session()
    r = s.post(f"{up}/login",
               data={"username": config.AUTH_USER, "password": config.AUTH_PASSWORD},
               allow_redirects=False, timeout=20)
    if r.status_code != 303:                       # 303 = login sukses (redirect ke /)
        raise RuntimeError(f"login live gagal (HTTP {r.status_code})")

    # Secret pairing: server hanya melayani mirror yang di-pair (di atas login + TLS).
    r = s.get(f"{up}/api/backup", timeout=180, stream=True,
              headers={"X-Mirror-Secret": config.MIRROR_SECRET})
    r.raise_for_status()
    tmp = dest + ".tmp"
    n = 0
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(65536):
            f.write(chunk)
            n += len(chunk)
    if n < 1024:                                    # DB SQLite valid minimal beberapa KB
        os.remove(tmp)
        raise RuntimeError(f"unduhan terlalu kecil ({n} byte) — batal, DB lama dipertahankan")
    # Buang sidecar WAL/SHM lama supaya tidak bentrok dengan DB baru.
    for sfx in ("-wal", "-shm"):
        try:
            os.remove(dest + sfx)
        except OSError:
            pass
    # SAFETY: sebelum menimpa, simpan DB lokal lama sebagai .prev (SATU rolling backup,
    # ditimpa tiap pull) DAN sebagai backup bertimestamp di data/backups/ (5 terakhir) —
    # rollback tersedia walau .prev sudah tertimpa pull berikutnya.
    if os.path.exists(dest):
        shutil.copy2(dest, dest + ".prev")
        _rotate_backup(dest)
    os.replace(tmp, dest)                           # ganti atomik
    return n


if __name__ == "__main__":
    saved = pull()
    print(f"[mirror] tersimpan {saved:,} byte -> {config.DB_PATH}")
