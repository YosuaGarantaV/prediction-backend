"""Audit 2026-07-30: satu definisi populasi resolusi + catch-up laporan tak dobel.

Tiga cacat yang ditemukan di live dan diperbaiki di sini:
  1. skill.performance_digest memakai `status='resolved'` mentah (populasi tercemar).
  2. repo.factor_scoreboard memakai penjaga tanggal WIB lama (membuang bracket sah).
  3. scheduler._learning_catchup jalan lagi walau laporan hari ini sudah ada (dobel LLM).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import market_calendar as _cal
from app import repo
from app.agents import skill


def _mk(conn, *, ts, resolved_at, direction="DOWN", outcome="win", horizon=3,
        reasoning="taruhan nyata", factors=None):
    conn.execute(
        "INSERT INTO predictions(ticker,direction,probability,horizon_days,entry_price,"
        "target_price,expected_pct,reasoning,factors_json,status,ts,resolved_at,actual_pct,"
        "outcome) VALUES('AAAA',?,55,?,100,98,-2,?,?,'resolved',?,?,-2.0,?)",
        (direction, horizon, reasoning,
         json.dumps({"signals": [{"name": factors}]}) if factors else None,
         ts, resolved_at, outcome))


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "t.db", raising=False)
    db._local = type(db._local)()        # paksa koneksi baru ke DB_PATH test
    db.init_db()
    return db.get_conn()


def _pasangan_lewat_sesi(now):
    """(ts, resolved_at) di dalam jendela 14 hari yang PASTI melewati satu sesi bursa.

    Dulu tes ini memaku now-3hari -> now-1hari. Saat dijalankan hari SENIN itu jadi
    Jumat -> Minggu: tak ada sesi bursa di antaranya, jadi resolved_predictions membuangnya
    (BENAR) dan digest balik '' -> tes merah. Jadi tes lama gagal/lolos tergantung HARI
    menjalankannya, bukan tergantung kode. Sekarang tanggalnya dicari, bukan diasumsikan.
    """
    for mundur in range(1, 12):
        res = now - timedelta(days=mundur)
        ts = res - timedelta(days=1)
        if _cal.has_new_session(ts, res):
            return ts, res
    raise AssertionError("tak ada pasangan tanggal yang melewati sesi bursa dalam 12 hari")


def test_digest_membuang_forecast_display_dan_resolusi_sesi_sama(conn):
    """Digest harus memakai populasi taruhan NYATA, bukan semua baris resolved."""
    now = datetime.now(timezone.utc)
    dibuat, ditutup = _pasangan_lewat_sesi(now)
    # 1 taruhan nyata KALAH, melewati sesi bursa
    _mk(conn, ts=dibuat.isoformat(), resolved_at=ditutup.isoformat(), outcome="loss")
    # 8 forecast display 1-hari MENANG -> harus DIABAIKAN (dulu mengangkat win-rate palsu)
    for _ in range(8):
        _mk(conn, ts=dibuat.isoformat(), resolved_at=ditutup.isoformat(),
            horizon=1, reasoning="prediksi 1-hari (besok) — forecast", outcome="win")
    conn.commit()

    txt = skill.performance_digest(days=14)
    # Populasi bersih = 1 baris, 0 menang. Kalau masih memakai query mentah -> 8/9 = 89%.
    assert "0/1" in txt, f"digest masih memakai populasi tercemar: {txt!r}"
    assert "8/9" not in txt and "89%" not in txt


def test_factor_scoreboard_tak_membuang_penutupan_sesi_sama(conn):
    """Prediksi dibuat pagi & ditutup sesudah closing di hari yang SAMA tetap sah."""
    # Jumat pagi WIB -> Jumat sesudah closing WIB (tanggal WIB sama, tapi sesi bursa sudah lewat)
    pagi = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)      # 09:00 WIB
    sore = datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc)     # 16:30 WIB, sesudah closing
    _mk(conn, ts=pagi.isoformat(), resolved_at=sore.isoformat(),
        outcome="win", factors="asing_jual")
    conn.commit()

    board = repo.factor_scoreboard(min_n=1)
    names = {b["factor"] for b in board}
    # Penjaga LAMA (date(resolved_at,'+7h') > date(ts,'+7h')) membuang baris ini -> board kosong.
    assert "asing_jual" in names, "penutupan sesi-sama yang sah masih dibuang dari papan skor"


def test_catchup_dilewati_bila_laporan_hari_ini_sudah_ada(tmp_path, monkeypatch):
    """Cron 16:00 sudah menulis laporan hari ini -> catch-up TIDAK boleh jalan lagi."""
    from app import scheduler

    monkeypatch.setattr(scheduler.config, "LOG_DIR", tmp_path, raising=False)
    # Laporan hari ini ada, tapi mtime-nya dibuat tua (>20 jam) supaya ambang lama LOLOS.
    f = tmp_path / f"report_{datetime.now():%Y%m%d}.md"
    f.write_text("laporan hari ini", encoding="utf-8")
    import os
    tua = datetime.now().timestamp() - 22 * 3600
    os.utime(f, (tua, tua))

    dipanggil = []
    monkeypatch.setattr(scheduler.report, "save_to_file",
                        lambda: dipanggil.append("report"))
    monkeypatch.setattr(scheduler.knowledge, "auto_tune",
                        lambda: dipanggil.append("auto_tune"))

    scheduler._learning_catchup()
    assert dipanggil == [], f"catch-up jalan dobel padahal laporan hari ini ada: {dipanggil}"
