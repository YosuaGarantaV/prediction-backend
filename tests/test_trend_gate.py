"""Cek gate DON'T-FIGHT-THE-TREND (python test_trend_gate.py). Data 2026-07-13: prediksi
lawan tren IHSG rugi sistematis -> FLAT, kecuali ada katalis berita spesifik emiten."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from app.agents import orchestrator as orch
from app.data import flow


def _mk(direction, prob=62):
    return {"ticker": "TST", "direction": direction, "probability": prob, "horizon_days": 3,
            "entry_price": 100.0, "target_price": 96.0, "expected_pct": -4.0,
            "action": "HOLD", "key_factors": []}


def _stub(uptrend, news=None):
    flow.ihsg_uptrend = lambda: (uptrend, 5900.0, 5844.0)
    orch.repo.news_for_ticker = lambda *a, **k: news or []
    orch.repo.log = lambda *a, **k: None


def test_down_in_uptrend_flattened():
    _stub(uptrend=True)                                   # pasar NAIK
    out = orch._trend_agreement_gate(_mk("DOWN"))
    assert out["direction"] == "FLAT", out               # DOWN lawan uptrend -> FLAT
    assert any("gate tren" in str(f) for f in out["key_factors"])
    print("DOWN @ uptrend -> FLAT ok")


def test_up_in_downtrend_flattened():
    _stub(uptrend=False)                                 # pasar TURUN
    out = orch._trend_agreement_gate(_mk("UP"))
    assert out["direction"] == "FLAT", out               # UP lawan downtrend (biang rugi n411) -> FLAT
    print("UP @ downtrend -> FLAT ok")


def test_with_trend_kept():
    _stub(uptrend=False)                                 # pasar TURUN
    out = orch._trend_agreement_gate(_mk("DOWN"))        # DOWN searah downtrend = EDGE 62.7%
    assert out["direction"] == "DOWN", out               # dipertahankan
    print("DOWN @ downtrend (searah) -> dipertahankan ok")


def test_news_catalyst_escape():
    _stub(uptrend=True, news=[{"tickers": "TST", "impact": -3, "title": "fraud"}])
    out = orch._trend_agreement_gate(_mk("DOWN"))        # DOWN lawan uptrend TAPI ada berita -3
    assert out["direction"] == "DOWN", out               # katalis spesifik -> boleh lawan tren
    print("lawan tren + katalis berita spesifik -> dipertahankan ok")


def test_no_ihsg_data_safe():
    flow.ihsg_uptrend = lambda: (False, 0.0, 0.0)        # data IHSG kurang
    out = orch._trend_agreement_gate(_mk("UP"))
    assert out["direction"] == "UP", out                 # tak ada data -> jangan gate (aman)
    print("data IHSG kurang -> gate non-aktif (aman) ok")


if __name__ == "__main__":
    test_down_in_uptrend_flattened()
    test_up_in_downtrend_flattened()
    test_with_trend_kept()
    test_news_catalyst_escape()
    test_no_ihsg_data_safe()
    print("ALL OK")
