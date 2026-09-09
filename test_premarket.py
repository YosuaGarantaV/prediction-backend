"""Cek briefing global semalam (python test_premarket.py) — tanpa jaringan/DB."""
from app import repo
from app.data import premarket


def _macro(d):
    repo.all_macro = lambda: [{"name": k, "change_pct": v} for k, v in d.items()]


def test_risk_on():
    _macro({"S&P 500": 1.5, "Nasdaq": 2.0, "Nikkei 225": 1.0, "VIX (Fear)": -8, "USD/IDR": -0.3})
    b = premarket.global_brief()
    assert b["score"] > 1.5 and "RISK-ON" in b["lean"], b
    print("risk-on ok")


def test_risk_off():
    # Wall St jatuh, rupiah melemah, VIX melonjak → RISK-OFF
    _macro({"S&P 500": -2.0, "Nasdaq": -2.5, "USD/IDR": 0.8, "VIX (Fear)": 15})
    b = premarket.global_brief()
    assert b["score"] < -1.5 and "RISK-OFF" in b["lean"], b
    print("risk-off ok")


def test_clamp():
    # VIX +50% TAK boleh mendominasi (clamp per-driver) → tetap masuk akal
    _macro({"S&P 500": 1.0, "VIX (Fear)": 50})
    b = premarket.global_brief()
    assert b["score"] <= 2.0, ("VIX ekstrem harus ter-clamp", b)
    print("clamp ok")


if __name__ == "__main__":
    test_risk_on()
    test_risk_off()
    test_clamp()
    print("ALL OK")
