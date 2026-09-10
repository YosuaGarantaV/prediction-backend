"""Cek post-mortem high-conf miss (orchestrator.diagnose_miss + _learn)."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from app import repo
from app.agents import orchestrator as orch


def test_sideways_vs_reversal(monkeypatch=None):
    repo.news_for_ticker = lambda *a, **k: []  # isolasi: tanpa berita

    # sideways: gerak ~0% padahal yakin
    s = orch.diagnose_miss({"direction": "UP", "probability": 80, "horizon_days": 3}, 0.0)
    assert "SIDEWAYS" in s, s

    # reversal: UP tapi turun
    r = orch.diagnose_miss({"direction": "UP", "probability": 85, "horizon_days": 3}, -3.5)
    assert "REVERSAL" in r, r

    # arah benar tapi target meleset (DOWN & memang turun, masih dihitung loss oleh threshold lain)
    t = orch.diagnose_miss({"direction": "DOWN", "probability": 70, "horizon_days": 3}, -0.4)
    assert "SIDEWAYS" in t, t  # |−0.4|<1 → sideways


def test_news_specific_vs_global():
    # (a) berita yg ter-tag saham ini → "berita SPESIFIK"
    repo.news_for_ticker = lambda *a, **k: [
        {"sentiment": "negative", "impact": -3, "tickers": "TEST", "title": "TEST gagal bayar utang"},
    ]
    s = orch.diagnose_miss({"ticker": "TEST", "direction": "UP", "probability": 78, "horizon_days": 3}, -4.2)
    assert "REVERSAL" in s and "SPESIFIK" in s, s

    # (b) hanya berita global generik (tak nyebut saham) → label "konteks global", bukan dituding penyebab
    repo.news_for_ticker = lambda *a, **k: [
        {"sentiment": "negative", "impact": -3, "tickers": "", "title": "Harga Bitcoin terkoreksi 4%"},
    ]
    s = orch.diagnose_miss({"ticker": "TEST", "direction": "DOWN", "probability": 80, "horizon_days": 3}, 0.0)
    assert "SIDEWAYS" in s and "konteks global" in s, s


if __name__ == "__main__":
    test_sideways_vs_reversal()
    test_news_specific_vs_global()
    print("OK test_miss: sideways/reversal + berita SPESIFIK vs konteks global.")
