"""Cek prediksi saat pasar tutup (python test_closed_cycle.py) — tanpa jaringan."""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import repo
from app.agents import orchestrator

WIB = timezone(timedelta(hours=7))


def test_trading_days():
    # Sabtu 12:00 → Minggu 12:00 (WIB) = 0 hari bursa (TAK boleh jatuh tempo di weekend)
    sat = datetime(2026, 6, 20, 5, 0, tzinfo=timezone.utc)   # 12:00 WIB Sabtu
    sun = sat + timedelta(days=1)
    assert repo._trading_days_elapsed(sat, sun) == 0, "weekend bukan hari bursa"
    # Jumat → Senin = 1 hari bursa (Senin)
    fri = datetime(2026, 6, 19, 5, 0, tzinfo=timezone.utc)   # Jumat WIB
    mon = fri + timedelta(days=3)
    assert repo._trading_days_elapsed(fri, mon) == 1, "Jum→Sen = 1 hari bursa"
    # Kamis → +6 hari (Rabu depan) = 4 hari bursa (Jum,Sen,Sel,Rab)
    thu = datetime(2026, 6, 18, 5, 0, tzinfo=timezone.utc)
    assert repo._trading_days_elapsed(thu, thu + timedelta(days=6)) == 4
    print("trading_days ok")


def test_closed_mode():
    orchestrator._CLOSED_STATE = Path(tempfile.mkdtemp()) / "cc.json"
    # pra-buka: pertama kali → jalan, kedua (hari sama) → None
    assert orchestrator._closed_mode("pre-open") == "pre-open"
    today = datetime.now(timezone.utc).astimezone(WIB).date().isoformat()
    orchestrator._save_closed_state({"last_preopen": today})
    assert orchestrator._closed_mode("pre-open") is None, "pra-buka cuma 1×/hari"

    # darurat: tanpa shock → None; dengan shock & cooldown lewat → 'emergency'
    orchestrator._CLOSED_STATE = Path(tempfile.mkdtemp()) / "cc2.json"
    orchestrator._fresh_shock = lambda: []
    assert orchestrator._closed_mode("closed") is None, "tanpa shock tak jalan"
    orchestrator._fresh_shock = lambda: [{"title": "RESESI", "impact": -3}]
    assert orchestrator._closed_mode("closed") == "emergency", "shock → darurat"
    # cooldown: baru saja jalan → None
    orchestrator._save_closed_state({"last_emergency": datetime.now(timezone.utc).isoformat()})
    assert orchestrator._closed_mode("closed") is None, "cooldown aktif"
    print("closed_mode ok")


if __name__ == "__main__":
    test_trading_days()
    test_closed_mode()
    print("ALL OK")
