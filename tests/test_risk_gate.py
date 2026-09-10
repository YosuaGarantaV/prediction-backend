"""Cek Risk Agent / pre_trade_checks (python test_risk_gate.py) — monkeypatch, tanpa DB nyata."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import json

import config
from app import repo
from app.trading import paper


def _setup(*, feats=None, fund=None, holding=None, positions=None, chg=0.0, fp=None):
    repo.get_quote = lambda t: {"change_pct": chg, "price": 100.0,
                                "features_json": json.dumps(feats or {})}
    repo.get_position = lambda t: holding
    repo.get_positions = lambda: positions if positions is not None else []
    repo.get_fundamentals_row = lambda t: fund or {}
    repo.last_sell_ts = lambda t: None
    repo.last_buy_ts = lambda t: None
    repo.get_cash = lambda: 40_000_000.0
    paper.mark_to_market = lambda: 60_000_000.0  # total 100 jt
    from app.data import idxflow
    idxflow.foreign_pressure = lambda t: fp
    repo.log = lambda *a, **k: None


def _buy(size=5.0):
    return {"ticker": "TEST", "action": "BUY", "entry_price": 100.0, "size_pct": size}


def test_veto_falling_knife():
    _setup(feats={"cum_change_3d": -15, "macd_hist": -1})
    v, why, _ = paper.pre_trade_checks(_buy())
    assert v == "VETO" and "falling knife" in why, (v, why)
    print("VETO falling knife ok")


def test_veto_valuation():
    _setup(fund={"per": 297, "pbv": 2})
    v, why, _ = paper.pre_trade_checks(_buy())
    assert v == "VETO" and "kemahalan" in why, (v, why)
    print("VETO valuasi ekstrem ok")


def test_veto_positions_full():
    _setup(positions=[{"ticker": f"T{i}"} for i in range(config.MAX_POSITIONS)])
    v, why, _ = paper.pre_trade_checks(_buy())
    assert v == "VETO" and "penuh" in why, (v, why)
    print("VETO posisi penuh ok")


def test_resize_over_room():
    # posisi TEST sudah Rp10 jt (10%); cap 15% → ruang 5%; minta 12% → RESIZE cap ~5
    _setup(holding={"qty": 100_000, "avg_price": 100.0, "opened_at": "2026-07-01"})
    v, why, cap = paper.pre_trade_checks(_buy(size=12))
    assert v == "RESIZE" and cap is not None and abs(cap - 5.0) < 0.2, (v, why, cap)
    print("RESIZE saat size > ruang ok")


def test_approve_normal():
    _setup(feats={"cum_change_3d": 1, "macd_hist": 0.5})
    v, why, cap = paper.pre_trade_checks(_buy())
    assert v == "APPROVE" and cap is None, (v, why)
    v, _, _ = paper.pre_trade_checks({"ticker": "TEST", "action": "HOLD"})
    assert v == "APPROVE"
    print("APPROVE normal + HOLD ok")


def test_veto_thin_edge():
    """Edge tipis vs fee: BUY exp < MIN_EDGE_PCT -> VETO; exp cukup -> APPROVE."""
    _setup()
    d = _buy()
    d["expected_pct"] = config.MIN_EDGE_PCT - 0.5
    v, why, _ = paper.pre_trade_checks(d)
    assert v == "VETO" and "edge tipis" in why, (v, why)
    d["expected_pct"] = config.MIN_EDGE_PCT + 0.5
    v, _, _ = paper.pre_trade_checks(d)
    assert v == "APPROVE", v
    print("VETO edge tipis vs fee ok")


def test_veto_sell_flat():
    """Audit 2026-09-08: 44 dari 54 SELL dipicu re-prediksi FLAT. FLAT = tak ada sinyal,
    dan round-trip-nya bergerak 0,00% sambil membayar fee 0,40%. DOWN (tesis rusak) tetap
    boleh jual, dan check_exits memakai sell(force=True) yang tak lewat gerbang ini."""
    _setup(holding={"qty": 1000, "avg_price": 100.0, "opened_at": "2026-09-01"})
    jual = {"ticker": "TEST", "action": "SELL", "entry_price": 100.0}
    v, why, _ = paper.pre_trade_checks({**jual, "direction": "FLAT"})
    assert v == "VETO" and "FLAT" in why, (v, why)
    v, why, _ = paper.pre_trade_checks({**jual, "direction": "DOWN"})
    assert v == "APPROVE", (v, why)
    # FLAT hasil tulis-ulang _proven_loss_gate tetap boleh keluar posisi — kalau tidak,
    # gerbang lengan-rugi mengunci posisi yang justru ingin ia lepas.
    v, why, _ = paper.pre_trade_checks({**jual, "direction": "FLAT",
                                        "_arm_closed_from": "DOWN"})
    assert v == "APPROVE", (v, why)
    print("VETO SELL saat arah FLAT ok")


def test_veto_beli_dua_kali_sehari():
    """17 entri ekstra terukur (COIN 3x/85 menit, JELI 6x sehari). Averaging lintas hari
    tetap boleh — yang diblokir cuma pengulangan tesis yang sama di hari yang sama."""
    from datetime import datetime, timedelta

    from app import repo as _repo
    pos = {"qty": 1000, "avg_price": 100.0, "opened_at": "2026-09-01"}
    _setup(holding=pos, feats={"cum_change_3d": 1, "macd_hist": 0.5})
    _repo.last_buy_ts = lambda t: datetime.now(_repo._WIB).isoformat()
    v, why, _ = paper.pre_trade_checks(_buy())
    assert v == "VETO" and "hari ini" in why, (v, why)

    _repo.last_buy_ts = lambda t: (datetime.now(_repo._WIB) - timedelta(days=2)).isoformat()
    v, why, _ = paper.pre_trade_checks(_buy())
    assert v in ("APPROVE", "RESIZE"), (v, why)
    print("VETO entri kedua di hari yang sama ok")


def test_risk_agent_never_throws():
    from app.agents import risk
    repo.get_quote = lambda t: (_ for _ in ()).throw(RuntimeError("db mati"))
    out = risk.assess(_buy())
    assert out["verdict"] == "APPROVE" and "error" in out["reason"], out
    print("risk.assess tahan error (APPROVE + re-check commit) ok")


if __name__ == "__main__":
    test_veto_falling_knife()
    test_veto_valuation()
    test_veto_positions_full()
    test_resize_over_room()
    test_approve_normal()
    test_veto_thin_edge()
    test_veto_sell_flat()
    test_veto_beli_dua_kali_sehari()
    test_risk_agent_never_throws()
    print("ALL OK")
