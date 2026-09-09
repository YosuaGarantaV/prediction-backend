"""Cek anti-churn cooldown re-entry (python test_paper.py) — tanpa DB nyata, pakai monkeypatch."""
from datetime import datetime, timezone, timedelta

import config
from app import repo
from app.trading import paper


def _setup(last_sell_age_h):
    """Palsukan repo: tak ada posisi, jual terakhir `last_sell_age_h` jam lalu."""
    repo.log = lambda *a, **k: None  # jangan menulis ke engine.log/DB produksi (bocor 2026-07-14)
    repo.get_quote = lambda t: {"change_pct": 0, "features_json": "{}"}
    repo.get_position = lambda t: None
    repo.get_positions = lambda: []  # pre_trade_checks kini cek posisi-penuh di jalur act
    repo.get_fundamentals_row = lambda t: {}
    ts = (datetime.now(timezone.utc) - timedelta(hours=last_sell_age_h)).isoformat()
    repo.last_sell_ts = lambda t: ts
    calls = []
    paper.buy = lambda *a, **k: calls.append(("buy", a, k))
    return calls


def test_cooldown_blocks_recent_resell():
    calls = _setup(last_sell_age_h=1)  # baru dijual 1 jam lalu (< cooldown 6j)
    paper.act_on_decision({"ticker": "TEST", "entry_price": 100, "action": "BUY",
                           "size_pct": 5, "reasoning": "x"}, None)
    assert not calls, "harusnya SKIP BUY (masih cooldown)"
    print("blokir re-entry dini ok")


def test_cooldown_allows_after_window():
    calls = _setup(last_sell_age_h=config.REENTRY_COOLDOWN_HOURS + 1)  # sudah lewat cooldown
    paper.act_on_decision({"ticker": "TEST", "entry_price": 100, "action": "BUY",
                           "size_pct": 5, "reasoning": "x"}, None)
    assert calls, "harusnya BOLEH BUY (cooldown lewat)"
    print("izinkan setelah cooldown ok")


def test_minhold_blocks_discretionary_churn():
    """Guardrail biaya (2026-07-13): jual DISKRESIONER < MIN_HOLD_H jam di zona noise DIBLOKIR;
    exit pengaman (force=True) & gerak besar TETAP jalan. Cegah fee-disaster 85%-churn."""
    import importlib
    importlib.reload(paper)
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()  # baru dibeli 1 jam
    repo.get_position = lambda t: {"qty": 1000, "avg_price": 100.0, "opened_at": fresh}
    repo.log = lambda *a, **k: None
    sold = []
    repo.record_trade = lambda t: sold.append(t) or 1
    repo.upsert_position = lambda *a, **k: None
    repo.get_cash = lambda: 1e7
    repo.snapshot_portfolio = lambda *a, **k: None
    paper.mark_to_market = lambda: 0.0
    repo.now_iso = lambda: fresh

    # 1) diskresioner, P/L +2% (zona noise) < min-hold → DIBLOKIR
    assert paper.sell("TEST", 102.0, reason="agen flip") is None, "harus blokir churn noise"
    assert not sold
    # 2) gerak BESAR +12% (di luar band 10) → boleh keluar walau muda
    assert paper.sell("TEST", 112.0, reason="lonjakan") is not None, "gerak besar harus boleh"
    # 3) force=True (stop-loss/take-profit check_exits) → SELALU jalan walau noise & muda
    repo.get_position = lambda t: {"qty": 1000, "avg_price": 100.0, "opened_at": fresh}
    assert paper.sell("TEST", 101.0, reason="auto-exit", force=True) is not None, "exit pengaman wajib jalan"
    print("min-hold blokir churn diskresioner, exit pengaman & gerak-besar tetap jalan ok")


def test_cumulative_position_cap():
    """Bug ANTM 2026-07-03: 6x BUY menumpuk 60% portofolio. Cap kumulatif: posisi ticker
    yang sudah >= 15% ekuitas -> BUY berikutnya di-skip; yang hampir penuh -> alokasi dipangkas."""
    import importlib
    importlib.reload(paper)                          # pulihkan buy asli (test lain mem-patch)
    repo.get_cash = lambda: 40_000_000.0
    paper.mark_to_market = lambda: 60_000_000.0      # total 100 jt
    repo.get_positions = lambda: [{"ticker": "ANTM"}]
    # posisi ANTM sudah Rp20 jt (20% > cap 15%) -> skip
    repo.get_position = lambda t: {"qty": 10000, "avg_price": 2000.0, "opened_at": "2026-07-01"}
    logs = []
    repo.log = lambda *a, **k: logs.append(a)
    out = paper.buy("ANTM", 2000.0, 10)
    assert out is None, "harusnya SKIP (posisi 20% > cap 15%)"
    assert any("cap" in str(a) for a in logs), logs
    print("cap kumulatif per-ticker ok")


if __name__ == "__main__":
    test_cooldown_blocks_recent_resell()
    test_cooldown_allows_after_window()
    test_minhold_blocks_discretionary_churn()
    test_cumulative_position_cap()
    print("ALL OK")
