"""Uji fokus & gaya trading + gate scraping hemat (jalankan: python test_focus.py)."""
import time

import config
from app import scheduler


def test_parse_focus():
    f = config._parse_focus("BBCA:invest, bbri, ANTM:scalp, x:swing, GOTO:ngawur")
    assert f == {"BBCA": "invest", "BBRI": "swing", "ANTM": "scalp", "GOTO": "swing"}, f
    assert config._parse_focus("") == {}
    # gaya → horizon terdefinisi semua
    for style in f.values():
        assert style in config.FOCUS_STYLES and style in config.FOCUS_REFRESH_MIN


def test_hemat_gate(monkeypatch_phase=None):
    calls = []
    fn = scheduler._hemat("uji", lambda: calls.append(1), 60)
    skip = scheduler._hemat("uji2", lambda: calls.append(2), None)

    from app.data import stocks
    orig = stocks.market_phase
    try:
        # pasar TUTUP: jalan 1x lalu di-skip dalam jendela 60 menit; skip-total tak pernah jalan
        stocks.market_phase = lambda now=None: "closed"
        fn(); fn(); fn()
        assert calls == [1], calls
        skip(); skip()
        assert calls == [1], calls
        # pasar BUKA: selalu jalan
        stocks.market_phase = lambda now=None: "open"
        fn(); fn()
        assert calls == [1, 1, 1], calls
        skip()
        assert calls == [1, 1, 1, 2], calls
        # kembali tutup: jendela baru saja di-refresh saat buka → tetap skip
        stocks.market_phase = lambda now=None: "weekend"
        fn()
        assert calls == [1, 1, 1, 2], calls
        # jendela kedaluwarsa → jalan lagi sekali
        scheduler._gate_last["uji"] = time.time() - 61 * 60
        fn()
        assert calls == [1, 1, 1, 2, 1], calls
    finally:
        stocks.market_phase = orig


def test_orchestrator_style_note():
    from app.agents import orchestrator
    for style in config.FOCUS_STYLES:
        assert style in orchestrator.STYLE_NOTE, f"STYLE_NOTE kurang: {style}"


if __name__ == "__main__":
    test_parse_focus()
    test_hemat_gate()
    test_orchestrator_style_note()
    print("test_focus: SEMUA LULUS")
