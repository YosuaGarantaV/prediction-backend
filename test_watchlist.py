"""Cek fitur pantauan (star/pin), gaya auto, kadaluarsa hari-bursa (python test_watchlist.py)."""
import sqlite3
from datetime import date

from app import market_calendar as cal
from app.agents import style


def test_calendar_skips_weekend_and_holiday():
    # Jumat 2026-07-10 + 1 hari bursa = Senin 13 (lompati Sabtu/Minggu)
    assert cal.add_trading_days(date(2026, 7, 10), 1) == date(2026, 7, 13)
    # Kamis 2026-04-02, Jumat 04-03 = Wafat Isa (libur) → +1 hari bursa = Senin 04-06
    assert cal.add_trading_days(date(2026, 4, 2), 1) == date(2026, 4, 6)
    # Senin libur (2026-06-01 Pancasila): Jumat 05-29 +1 = Selasa 06-02 (lompati weekend+Senin libur)
    assert cal.add_trading_days(date(2026, 5, 29), 1) == date(2026, 6, 2)
    assert not cal.is_trading_day(date(2026, 8, 17))   # Kemerdekaan
    assert cal.is_trading_day(date(2026, 7, 13))       # Senin biasa
    print("kalender: skip weekend + libur nasional ok")


def test_style_classifier():
    assert style.classify_style({"volatility_20d": 5.0}) == "scalp"          # fluktuatif
    assert style.classify_style({"volatility_20d": 2.0, "above_sma20": True,
                                 "momentum_10d": 3}, {"roe": 18}) == "invest"  # tenang+quality
    assert style.classify_style({"volatility_20d": 3.2, "above_sma20": True,
                                 "momentum_10d": 1}) == "swing"               # default
    s, h = style.style_and_horizon({"volatility_20d": 5.0})
    assert s == "scalp" and h == 1                                            # horizon dari FOCUS_STYLES
    print("klasifikasi gaya auto ok")


def test_save_prediction_sets_expiry():
    from app import repo, db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    db._add_column(conn, "predictions", "expires_at", "TEXT")
    db._add_column(conn, "predictions", "style", "TEXT")
    conn.commit()
    repo.get_conn = lambda: conn
    db.get_conn = lambda: conn        # tx() (di db) pakai db.get_conn — wajib dipatch juga
    pid = repo.save_prediction({"ticker": "TST", "direction": "UP", "probability": 60,
                                "horizon_days": 1, "entry_price": 100, "target_price": 101,
                                "expected_pct": 1, "reasoning": "x", "style": "scalp"})
    row = conn.execute("SELECT expires_at, style FROM predictions WHERE id=?", (pid,)).fetchone()
    assert row["expires_at"], "expires_at harus terisi (hari bursa)"
    assert row["style"] == "scalp", row["style"]
    # expires_at = hari bursa >= besok (bukan hari ini/weekend)
    exp = date.fromisoformat(row["expires_at"])
    assert cal.is_trading_day(exp) and exp > date.today(), exp
    print("save_prediction set kadaluarsa hari-bursa + gaya ok")


if __name__ == "__main__":
    test_calendar_skips_weekend_and_holiday()
    test_style_classifier()
    test_save_prediction_sets_expiry()
    print("ALL OK")
