"""Ambil arus asing RESMI IDX (Trading Summary) di LOKAL lalu DORONG ke server live.

Kenapa: IDX di balik Cloudflare memblok IP DATACENTER (Oracle VPS) total (body=0), tapi IP
RESIDENSIAL (laptop) lolos (curl_cffi impersonate Chrome). Jadi live tak bisa fetch sendiri —
data diambil di sini lalu di-scp ke live. Data END-OF-DAY, cukup dijalankan sekali/hari sesudah
bursa tutup (atau kapan pun kamu buka lokal).

Jalankan:  python tools/push_idxflow.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # jalan dari CWD mana pun (scheduled task tak set folder kerja)

import config
from app.data import idxflow

# Alamat & kunci server TIDAK dipatok di kode: nilainya beda per pemasangan, dan repo ini
# dibagikan. Isi lewat .env (PUSH_HOST=user@ip, PUSH_KEY=path kunci, PUSH_REMOTE=tujuan).
# Path relatif diselesaikan terhadap ROOT: scheduled task Windows tak menyetel folder kerja,
# jadi ".ssh/kunci" apa adanya akan menunjuk ke tempat yang salah.
KEY = (ROOT / config.PUSH_KEY) if config.PUSH_KEY else ROOT / ".ssh" / "id_ed25519"
HOST = config.PUSH_HOST
REMOTE = config.PUSH_REMOTE
TRIES = 3          # fetch IDX gagal ~1 dari 2 kali (diamati 2026-07-28)
JEDA = 60          # detik antar percobaan


def _sesi(data: dict) -> str:
    """Tanggal sesi bursa isi data ('' kalau kosong) — semua baris satu tanggal."""
    return next(iter(data.values()))["date"] if data else ""


def main() -> None:
    if not HOST:
        sys.exit("PUSH_HOST kosong di .env — isi user@host tujuan dulu (lihat .env.example)")
    src = config.DATA_DIR / "foreign_flow.json"
    # File lokal = catatan sesi yang TERAKHIR BERHASIL didorong (ditulis sesudah scp sukses).
    terkirim = _sesi(json.loads(src.read_text(encoding="utf-8"))) if src.exists() else ""

    # fetch_foreign() mundur sampai 6 hari cari sesi yg ada datanya — itu benar utk libur bursa,
    # TAPI satu kegagalan transien pd tanggal terbaru bikin ia diam-diam balik sesi lama dan
    # push lama mendorongnya sbg "segar" (27 Jul: live dapat sesi 23 Jul, panel tetap hijau
    # karena ambangnya mtime file). Jadi: coba beberapa kali, ambil sesi TERBARU, dan tolak
    # mendorong apa pun yg tidak lebih baru dari yg sudah terkirim.
    best: dict = {}
    for percobaan in range(TRIES):
        got = idxflow.fetch_foreign()
        if _sesi(got) > _sesi(best):
            best = got
        if _sesi(best) > terkirim:
            break
        if percobaan < TRIES - 1:
            time.sleep(JEDA)

    if not best:
        sys.exit(f"[idxflow] fetch lokal gagal/kosong {TRIES}x — cek koneksi / Cloudflare")
    if _sesi(best) <= terkirim:
        print(f"[idxflow] sesi terbaru {_sesi(best)} = yang sudah terkirim — tidak mendorong")
        return

    tmp = src.with_name(src.name + ".new")
    tmp.write_text(json.dumps(best), encoding="utf-8")
    subprocess.run(
        ["scp", "-i", str(KEY), "-o", "StrictHostKeyChecking=accept-new", str(tmp),
         f"{HOST}:{REMOTE}"],
        check=True)
    tmp.replace(src)   # baru dicatat sesudah scp sukses → scp gagal tak memblok push berikutnya
    print(f"[idxflow] {len(best)} saham arus asing resmi sesi {_sesi(best)} didorong ke live")


if __name__ == "__main__":
    main()
