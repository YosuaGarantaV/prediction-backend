"""Pagu token harian (app.agents.budget) + sambung jawaban yang kepotong (llm._sambung)."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import config
from app import repo
from app.agents import budget, llm

WIB = timezone(timedelta(hours=7))


def _db_kosong():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE agent_logs(id INTEGER PRIMARY KEY, ts TEXT, agent TEXT, "
                 "phase TEXT, level TEXT, ticker TEXT, message TEXT, payload_json TEXT)")
    conn.commit()
    repo.get_conn = lambda: conn
    repo.log = lambda *a, **k: None
    budget._reset_untuk_test()
    return conn


def _pakai(conn, total, geser_jam=0):
    ts = (datetime.now(timezone.utc) + timedelta(hours=geser_jam)).isoformat()
    conn.execute("INSERT INTO agent_logs(ts,agent,phase,payload_json) VALUES (?,?,?,?)",
                 (ts, "llm", "usage", json.dumps({"total_tokens": total})))
    conn.commit()
    budget._reset_untuk_test()


def test_pagu_menghitung_dari_agent_logs():
    conn = _db_kosong()
    config.DAILY_TOKEN_BUDGET = 1000
    assert budget.sisa() == 1000 and not budget.habis()
    _pakai(conn, 600)
    assert budget.sisa() == 400 and not budget.habis()
    _pakai(conn, 500)
    assert budget.sisa() == 0 and budget.habis()


def test_catat_menambah_tanpa_query_ulang():
    _db_kosong()
    config.DAILY_TOKEN_BUDGET = 1000
    budget.catat(999)
    assert budget.sisa() == 1 and not budget.habis()
    budget.catat(1)
    assert budget.habis()


def test_pagu_nol_berarti_tanpa_batas():
    _db_kosong()
    config.DAILY_TOKEN_BUDGET = 0
    budget.catat(10 ** 9)
    assert not budget.habis(), "pagu 0 = tanpa batas"


def test_hari_dihitung_menurut_wib():
    _db_kosong()
    assert budget.hari_wib() == datetime.now(WIB).strftime("%Y-%m-%d")
    # Batas bawah query = tengah malam WIB, yang di UTC jatuh 7 jam lebih awal.
    batas = datetime.fromisoformat(budget._batas_utc("2026-09-10"))
    assert batas.astimezone(WIB).strftime("%Y-%m-%d %H:%M") == "2026-09-10 00:00"


def test_habis_menolak_panggilan_sebagai_cooling():
    """_Habis WAJIB turunan _Cooling supaya chain melewati provider tanpa menganggapnya rusak."""
    _db_kosong()
    config.DAILY_TOKEN_BUDGET = 10
    budget.catat(99)
    assert issubclass(llm._Habis, llm._Cooling)
    try:
        llm._create(object(), "nara", retries=0, model="m", messages=[])
    except llm._Cooling as e:
        assert "pagu" in str(e).lower()
    else:
        raise AssertionError("panggilan wajib ditolak saat pagu habis")


def test_jawaban_kepotong_disambung():
    """finish_reason='length' = model kehabisan jatah, bukan selesai. Harus disambung."""
    _db_kosong()
    config.DAILY_TOKEN_BUDGET = 0
    sisa = ["", " dan ini lanjutannya."]

    class _M:
        def __init__(self, c): self.content = c
    class _C:
        def __init__(self, c, fr): self.message, self.finish_reason = _M(c), fr
    class _R:
        def __init__(self, c, fr): self.choices, self.usage = [_C(c, fr)], None

    def fake_create(client, key, **kw):
        return _R(sisa[1], "stop")

    llm._create = fake_create
    resp = _R("ini tesis yang", "length")
    _, teks = llm._sambung(object(), "nara", {"model": "m", "messages": []}, resp, "ini tesis yang")
    assert teks == "ini tesis yang dan ini lanjutannya.", teks


def test_yang_selesai_tidak_disambung():
    _db_kosong()
    dipanggil = []
    llm._create = lambda *a, **k: dipanggil.append(1)

    class _M:
        content = "tesis utuh"
    class _C:
        message, finish_reason = _M(), "stop"
    class _R:
        choices, usage = [_C()], None

    _, teks = llm._sambung(object(), "nara", {"model": "m", "messages": []}, _R(), "tesis utuh")
    assert teks == "tesis utuh" and not dipanggil, "jangan sambung jawaban yang sudah selesai"
