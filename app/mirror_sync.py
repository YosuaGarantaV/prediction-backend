"""Mirror INKREMENTAL: tarik /api/mirror (payload kecil, gzip) dari server live secara
berkala lalu upsert ke DB lokal — DB lokal tetap segar tanpa mengunduh ulang DB penuh.

Melengkapi sync_from_live.pull() (snapshot penuh, hanya saat start). Dipakai run.py HANYA
pada mode mirror (RUN_ENGINE=0 + MIRROR_UPSTREAM di-set); di server live RUN_ENGINE=1 ->
loop ini tidak pernah jalan. Satu kanal, dua gerbang: sesi login + header X-Mirror-Secret.

Kegagalan terisolasi: satu tabel gagal -> rollback tabel itu saja, tabel lain tetap masuk;
satu tick gagal (live down / sesi kadaluarsa) -> log, login ulang di tick berikutnya.
"""
from __future__ import annotations

import os
import threading
import time

import requests

import config
from app import db

# Tabel STATE kecil: di-upsert per PRIMARY KEY (INSERT OR REPLACE) — nilai terbaru menang,
# TAPI baris lama TIDAK PERNAH dihapus (permintaan user: lokal = arsip permanen, "no timpah").
# Efek: posisi yang ditutup / ticker di-un-star tetap tersimpan di lokal (basi tapi tak hilang).
STATE_TABLES = ("quotes", "positions", "watchlist", "macro", "flow")
# File data non-DB (universe/IPO/dead-list dsb) yang ikut di-mirror dari /api/datafiles.
# HARUS subset whitelist server — nama di luar daftar ini tidak pernah ditulis ke disk.
DATA_FILES = ("idx_universe.txt", "discovered_ipos.txt", "discovered_at.json",
              "upcoming_ipos.json", "dead_tickers.json", "foreign_flow.json",
              "anomaly_snap.json", "model.json", "backtest.json")
# Tabel APPEND: INSERT OR REPLACE by PRIMARY KEY -> baris baru masuk, update tertangkap
# (mis. prediksi open -> resolved), sejarah lama yang sudah ada di lokal TIDAK dihapus.
APPEND_TABLES = ("portfolio", "trades", "predictions", "news",
                 "agent_logs", "conversations", "lessons", "prices")


def _login() -> requests.Session:
    """Sesi login baru ke live (username/password sama dengan dashboard)."""
    s = requests.Session()
    r = s.post(f"{config.MIRROR_UPSTREAM}/login",
               data={"username": config.AUTH_USER, "password": config.AUTH_PASSWORD},
               allow_redirects=False, timeout=20)
    if r.status_code != 303:                      # 303 = login sukses (redirect ke /)
        raise RuntimeError(f"login live gagal (HTTP {r.status_code})")
    return s


def _upsert(conn, table: str, rows: list[dict], replace_all: bool) -> None:
    """Terapkan satu tabel dalam SATU transaksi. Kolom dibangun dinamis dari keys row
    (SELECT * di server) -> schema-generic, tanpa daftar kolom hardcode."""
    try:
        if replace_all:
            conn.execute(f"DELETE FROM {table}")
        for row in rows:
            cols = list(row.keys())
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [row[c] for c in cols])
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def pull_incremental(session: requests.Session) -> dict[str, int]:
    """GET /api/mirror -> upsert semua tabel. Return {tabel: jumlah baris diterima}.
    Raise bila HTTP gagal (401 sesi kadaluarsa / 403 secret / live down) — pemanggil
    login ulang. Kegagalan per-tabel TIDAK menggagalkan tabel lain (log & lanjut)."""
    r = session.get(f"{config.MIRROR_UPSTREAM}/api/mirror", timeout=60,
                    headers={"X-Mirror-Secret": config.MIRROR_SECRET})
    r.raise_for_status()
    data = r.json()
    conn = db.get_conn()          # koneksi per-thread khusus thread sync ini
    counts: dict[str, int] = {}
    for table in STATE_TABLES + APPEND_TABLES:
        rows = data.get(table)
        if rows is None:          # server versi lama / tabel tak dikirim -> lewati
            continue
        try:
            _upsert(conn, table, rows, replace_all=False)   # additive-only: JANGAN hapus baris lokal
            counts[table] = len(rows)
        except Exception as e:
            print(f"[mirror] tabel {table} gagal upsert: {e}", flush=True)
    return counts


def pull_datafiles(session: requests.Session) -> int:
    """GET /api/datafiles -> tulis file data (universe/IPO/dead-list dsb) ke data/ lokal,
    atomik per file (tulis .tmp lalu os.replace) — file setengah-tertulis tak pernah
    terbaca engine. Hanya nama dalam DATA_FILES yang dipercaya. Return jumlah file."""
    r = session.get(f"{config.MIRROR_UPSTREAM}/api/datafiles", timeout=60,
                    headers={"X-Mirror-Secret": config.MIRROR_SECRET})
    r.raise_for_status()
    data = r.json()
    n = 0
    for name in DATA_FILES:
        text = data.get(name)
        if not isinstance(text, str) or not text:
            continue
        dest = config.DATA_DIR / name
        tmp = str(dest) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, dest)
            n += 1
        except Exception as e:  # noqa: BLE001 — 1 file gagal tak menggagalkan sisanya
            print(f"[mirror] file {name} gagal ditulis: {e}", flush=True)
    return n


def run_loop() -> None:
    """Loop daemon: sync inkremental tiap MIRROR_INTERVAL detik (lantai 15 dtk —
    jangan menghantam server live). Tick pertama langsung jalan."""
    interval = max(15, config.MIRROR_INTERVAL)
    session: requests.Session | None = None
    while True:
        stamp = time.strftime("%H:%M:%S")
        try:
            if session is None:
                session = _login()
            counts = pull_incremental(session)
            try:
                nfiles = pull_datafiles(session)
            except Exception as e:  # noqa: BLE001 — file gagal ≠ DB sync gagal
                nfiles = 0
                print(f"[mirror] {stamp} sync file data gagal: {e}", flush=True)
            print(f"[mirror] {stamp} sync inkremental OK: {sum(counts.values())} baris "
                  f"(quotes={counts.get('quotes', 0)}, prices={counts.get('prices', 0)}, "
                  f"predictions={counts.get('predictions', 0)}) + {nfiles} file data",
                  flush=True)
        except Exception as e:
            session = None        # paksa login ulang pada tick berikutnya
            print(f"[mirror] {stamp} sync inkremental gagal: {e}", flush=True)
        time.sleep(interval)


def start_background() -> threading.Thread:
    """Mulai loop sync sebagai daemon thread (mati otomatis bersama proses utama)."""
    t = threading.Thread(target=run_loop, daemon=True, name="mirror-sync")
    t.start()
    return t
