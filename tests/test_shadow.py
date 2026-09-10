"""Papan skor ablasi (app.agents.shadow) — jalankan: python test_shadow.py

Yang dijaga di sini persis cacat yang bikin H1 Bab IV tak bisa ditafsirkan: kalau tiga
lengan boleh menilai kandidat yang berbeda, atau boleh dinilai dengan ambang menang yang
berbeda, tabel perbandingannya kembali membandingkan populasi, bukan kemampuan.
"""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import os
import tempfile

import config
from app import db, repo
from app.agents import orchestrator as orc, shadow

_QUOTE = {"ticker": "BBCA", "price": 1000.0, "volume": 1e6, "features_json": "{}"}


def _setup(monkey_llm=None):
    # DB sementara di-set DI DALAM test, bukan saat impor: conftest mengembalikan global
    # modul sesudah tiap test, tapi efek impor terjadi saat KOLEKSI dan akan membajak DB
    # seluruh suite (terukur: 6 test lain gagal OperationalError).
    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "shadow_test.db")
    db._local = type(db._local)()
    db.init_db()
    repo.get_quote = lambda t: dict(_QUOTE, ticker=t)
    repo.get_position = lambda t: None
    repo.get_cash = lambda: 1e8
    shadow._llm1 = monkey_llm or (
        lambda t, q: {"direction": "DOWN", "probability": 60, "horizon_days": 3})
    from app.agents import heuristic
    heuristic.decide = lambda t, q, pos, cash, force_horizon=None: {
        "direction": "FLAT", "probability": 52, "horizon_days": 3, "score": 0.1}


def test_tiga_lengan_satu_batch():
    """Satu kandidat → tiga baris, batch sama, harga masuk & tanggal sama."""
    _setup()
    n = shadow.record("BBCA", {"direction": "UP", "probability": 70, "horizon_days": 3})
    assert n == 3, n
    rows = [dict(r) for r in db.get_conn().execute("SELECT * FROM shadow")]
    assert {r["arm"] for r in rows} == set(shadow.ARMS)
    assert len({r["batch"] for r in rows}) == 1
    assert {r["entry_price"] for r in rows} == {1000.0}
    assert {r["ticker"] for r in rows} == {"BBCA"}
    print("ok: tiga lengan satu batch")


def test_lengan_llm_gagal_tak_menjatuhkan_sisanya():
    """Provider mati → lubang jujur (2 lengan), bukan baris yang ditulis model lain."""
    def _boom(t, q):
        raise RuntimeError("provider mati")

    _setup(_boom)
    n = shadow.record("BBCA", {"direction": "UP", "probability": 70, "horizon_days": 3})
    assert n == 2, n
    arms = {r["arm"] for r in db.get_conn().execute("SELECT arm FROM shadow")}
    assert arms == {"mas", "heur"}, arms
    print("ok: lengan llm1 gagal → lubang, bukan substitusi")


def test_ambang_menang_sama_dengan_produksi():
    """shadow.resolve_due WAJIB memakai orc.is_win — bukan salinan ambang sendiri."""
    _setup()
    shadow.record("BBCA", {"direction": "UP", "probability": 70, "horizon_days": 3})
    db.get_conn().execute("UPDATE shadow SET expires_at='2000-01-01', ts='2000-01-01T00:00:00+00:00'")
    db.get_conn().commit()
    repo.get_quote = lambda t: dict(_QUOTE, ticker=t, price=1004.0)  # +0,4% → di dalam band
    shadow.resolve_due()
    got = {r["arm"]: r["outcome"] for r in db.get_conn().execute(
        "SELECT arm, outcome FROM shadow")}
    # +0,4%: UP KALAH (butuh >0,5%), DOWN kalah, FLAT menang (|0,4| <= 1,75) — sama persis
    # dengan orchestrator.is_win yang menilai baris produksi.
    assert got["mas"] == "loss" and got["llm1"] == "loss" and got["heur"] == "win", got
    for d, pct in (("UP", 0.6), ("DOWN", -0.6), ("FLAT", 0.4)):
        assert orc.is_win(d, pct), (d, pct)
    assert not orc.is_win("UP", 0.4)
    print("ok: ambang menang satu sumber")


def test_scoreboard_hanya_batch_lengkap():
    """Batch yang salah satu lengannya kosong TIDAK boleh ikut — itu populasi beda lagi."""
    _setup()
    shadow.record("BBCA", {"direction": "UP", "probability": 70, "horizon_days": 3})
    shadow._llm1 = lambda t, q: None
    shadow.record("BMRI", {"direction": "UP", "probability": 70, "horizon_days": 3})
    db.get_conn().execute("UPDATE shadow SET outcome='win', direction='UP', "
                          "resolved_at='2026-09-06T00:00:00+00:00'")
    db.get_conn().commit()
    sb = shadow.scoreboard()
    assert sb["batch_total"] == 2 and sb["batch_lengkap"] == 1, sb
    assert all(v["n"] == 1 for v in sb["lengan"].values()), sb
    print("ok: scoreboard hanya batch lengkap")


def test_record_many_paralel():
    """Perekaman banyak kandidat tidak boleh kehilangan satu batch pun saat paralel."""
    _setup()
    n = shadow.record_many([("BBCA", {"direction": "UP", "probability": 70, "horizon_days": 3}),
                            ("BMRI", {"direction": "DOWN", "probability": 61, "horizon_days": 5}),
                            ("PGAS", {"direction": "UP", "probability": 55, "horizon_days": 2})])
    assert n == 9, n
    batches = {r["batch"] for r in db.get_conn().execute("SELECT batch FROM shadow")}
    assert len(batches) == 3, batches
    print("ok: record_many paralel")


if __name__ == "__main__":
    test_tiga_lengan_satu_batch()
    test_lengan_llm_gagal_tak_menjatuhkan_sisanya()
    test_ambang_menang_sama_dengan_produksi()
    test_scoreboard_hanya_batch_lengkap()
    test_record_many_paralel()
    print("SEMUA OK")
