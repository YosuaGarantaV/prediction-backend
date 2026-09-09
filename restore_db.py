"""Pulihkan DB lokal dari backup (rollback anti-kehilangan-data).

Sumber: data/backups/prediction-YYYYmmdd-HHMMSS.db (5 terakhir, dibuat otomatis
tiap pull snapshot dari live) + data/prediction.db.prev (rolling tunggal).

Pakai:
  python restore_db.py        # daftar backup yang tersedia (terbaru dulu)
  python restore_db.py 2      # pulihkan backup nomor 2 dari daftar

DB sekarang disimpan dulu sebagai prediction.db.before-restore (undo tersedia).
Hentikan aplikasi (tutup jendela start.bat) sebelum restore.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
from datetime import datetime

import config
from sync_from_live import BACKUP_DIR


def _candidates() -> list[str]:
    """Daftar file backup, terbaru dulu. .prev ikut di akhir bila ada."""
    out = sorted((str(p) for p in BACKUP_DIR.glob("prediction-*.db")), reverse=True) \
        if BACKUP_DIR.exists() else []
    prev = str(config.DB_PATH) + ".prev"
    if os.path.exists(prev):
        out.append(prev)
    return out


def _fmt(path: str) -> str:
    st = os.stat(path)
    when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    return f"{os.path.basename(path):40s} {st.st_size / 1e6:8.1f} MB  {when}"


def _app_running() -> bool:
    """True kalau engine/mirror lokal masih hidup (kunci singleton :PORT+1 terpakai)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", config.PORT + 1))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main() -> None:
    cands = _candidates()
    if not cands:
        print("Tidak ada backup di", BACKUP_DIR, "maupun .prev — belum pernah sync?")
        return
    if len(sys.argv) < 2:
        print(f"Backup tersedia (restore: python restore_db.py <nomor>):")
        for i, p in enumerate(cands, 1):
            print(f"  {i}. {_fmt(p)}")
        return
    try:
        pick = cands[int(sys.argv[1]) - 1]
    except (ValueError, IndexError):
        print(f"Nomor tidak valid. Pilih 1..{len(cands)} (lihat: python restore_db.py)")
        sys.exit(1)
    if _app_running():
        print("Aplikasi masih JALAN (port kunci terpakai) — hentikan dulu "
              "(tutup jendela start.bat), lalu ulangi.")
        sys.exit(1)
    dest = str(config.DB_PATH)
    if os.path.exists(dest):
        shutil.copy2(dest, dest + ".before-restore")   # undo point
    for sfx in ("-wal", "-shm"):                       # sidecar lama tak boleh nyangkut
        try:
            os.remove(dest + sfx)
        except OSError:
            pass
    shutil.copy2(pick, dest)
    print(f"OK: {os.path.basename(pick)} -> {dest}")
    print(f"DB sebelumnya tersimpan sebagai {os.path.basename(dest)}.before-restore")


if __name__ == "__main__":
    main()
