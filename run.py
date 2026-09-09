"""Entrypoint: inisialisasi DB, mulai scheduler 24 jam, jalankan web dashboard.

Jalankan:  python run.py
Lalu buka: http://localhost:8800
"""
from __future__ import annotations

import socket
import sys

import uvicorn

# Konsol Windows default cp1252 → paksa UTF-8 agar log/teks tidak crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import config
from app import db, scheduler
from app.server import app

# Pegang seumur hidup proses (jangan di-GC) → kunci instance-tunggal tetap aktif.
_singleton_lock: socket.socket | None = None


def _acquire_singleton_lock() -> None:
    """Cegah ENGINE GANDA. Dulu 2 instance bisa jalan bareng → tiap scheduler bombardir
    provider → kuota habis 2× lebih cepat (instance ke-2 gagal bind 8800 tapi scheduler-nya
    terlanjur nyala). Kunci port lokal: instance ke-2 gagal bind → keluar SEBELUM scheduler nyala."""
    global _singleton_lock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", config.PORT + 1))  # port kunci (8801), terpisah dari server (8800)
    except OSError:
        print(f"⛔ Engine LAIN sudah jalan (kunci :{config.PORT + 1} terpakai). "
              f"Instance ini berhenti — anti-duplikat.")
        sys.exit(0)
    s.listen(1)
    _singleton_lock = s  # tahan referensi → socket tetap ke-bind seumur proses


def main():
    _acquire_singleton_lock()  # WAJIB paling awal: sebelum scheduler, cegah duplikat
    # Mode MIRROR: tarik snapshot DB live SEBELUM DB dibuka (cadangan tahan-mati). Gagal tarik
    # (live mati/tak terjangkau) → non-fatal: pakai DB lokal terakhir yang tersimpan.
    if config.MIRROR_ON_START and config.MIRROR_UPSTREAM:
        try:
            from sync_from_live import pull
            n = pull(str(config.DB_PATH))
            print(f"[mirror] DB live tersalin ({n:,} byte) dari {config.MIRROR_UPSTREAM}")
        except Exception as e:
            print(f"[mirror] GAGAL tarik dari live ({e}) — pakai DB lokal yang ada.")
    db.init_db()
    # Resume: tampilkan state yang TERSIMPAN dari sesi lalu (bukti engine "ingat").
    from app import repo
    pf = repo.latest_portfolio()
    pos = repo.get_positions()
    # RUN_ENGINE=0 (mode mirror/cadangan lokal) → JANGAN nyalakan scheduler LLM.
    if config.RUN_ENGINE:
        scheduler.start()
    elif config.MIRROR_UPSTREAM:
        # Mode mirror BERJALAN: setelah snapshot penuh di atas, jaga DB lokal tetap segar
        # via sync INKREMENTAL /api/mirror (payload kecil) tiap MIRROR_INTERVAL detik.
        # Di live RUN_ENGINE=1 -> cabang ini tak pernah jalan (tak sync dari diri sendiri).
        from app import mirror_sync
        mirror_sync.start_background()
        print(f"[mirror] sync inkremental aktif tiap {max(15, config.MIRROR_INTERVAL)} dtk "
              f"dari {config.MIRROR_UPSTREAM}")
    mode = ("LLM (NVIDIA NIM)" if config.USE_LLM else "Heuristik lokal") \
        if config.RUN_ENGINE else "MATI (mirror/cadangan, read-only)"
    print("=" * 60)
    print("  SAHAM IDX PREDICTION ENGINE")
    print(f"  Mode agen : {mode}")
    print(f"  Dashboard : http://localhost:{config.PORT}")
    print(f"  Lanjut    : Rp{pf['total']:,.0f} (P/L {pf['pnl_pct']:+.2f}%) | "
          f"{len(pos)} posisi: {', '.join(p['ticker'] for p in pos) or '-'}")
    print("=" * 60)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
