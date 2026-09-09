"""Supervisor: jalankan run.py, AUTO-RESTART kalau mati tak normal.

curl_cffi (yfinance) kadang crash NATIVE di Windows saat beban tinggi → proses mati
exit-4, tak bisa di-try/except dari Python. Solusinya: induk yang memantau & relaunch.
DB (SQLite) persist, jadi restart = lanjut, bukan mulai ulang.

Jalankan:  python serve.py   (ganti `python run.py` untuk pemakaian 24 jam)
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PY = BASE / ".venv" / "Scripts" / "python.exe"
PYEXE = str(PY) if PY.exists() else sys.executable

MAX_BACKOFF = 30


def main() -> None:
    fails = 0
    while True:
        start = time.time()
        code = subprocess.call([PYEXE, "run.py"], cwd=str(BASE))
        if code == 0:
            print("[supervisor] engine berhenti normal (exit 0). Keluar.")
            return
        # crash. Kalau sempat hidup lama, reset backoff (bukan crash-loop).
        if time.time() - start > 120:
            fails = 0
        fails += 1
        wait = min(MAX_BACKOFF, 5 * fails)
        print(f"[supervisor] engine MATI (exit {code}) — restart dalam {wait} dtk "
              f"(kegagalan ke-{fails})...")
        time.sleep(wait)


if __name__ == "__main__":
    main()
