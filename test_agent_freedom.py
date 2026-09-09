"""Cek untuk perubahan 2026-09-02: tool penemuan pola, peredam log, mode tanpa-fallback,
dan suara dewan yang tak lagi hilang. Tiap test GAGAL kalau perilaku lama kembali.

Jalankan: python -m pytest test_agent_freedom.py -q
"""
import json

import config
from app import repo
from app.agents import analyst, council, llm


# ---------------------------------------------------------------- screen_market
def _fake_quotes():
    """Tiga saham: satu oversold-volume-ramai, satu normal, satu oversold tapi ILIKUID."""
    return [
        {"ticker": "AAAA", "price": 1000, "change_pct": -3.2, "features_json": json.dumps(
            {"rsi14": 24.0, "vol_vs_avg": 3.1, "above_sma20": 0, "momentum_10d": -8.0})},
        {"ticker": "BBBB", "price": 2000, "change_pct": 0.4, "features_json": json.dumps(
            {"rsi14": 55.0, "vol_vs_avg": 0.9, "above_sma20": 1, "momentum_10d": 2.0})},
        {"ticker": "CCCC", "price": 60, "change_pct": -9.0, "features_json": json.dumps(
            {"rsi14": 19.0, "vol_vs_avg": 5.0, "above_sma20": 0, "momentum_10d": -20.0})},
    ]


def _stub_universe():
    repo.all_quotes = _fake_quotes
    repo.liquid_tickers = lambda *a, **k: {"AAAA", "BBBB"}   # CCCC sengaja tak likuid


def test_screen_market_menemukan_pola():
    """Agen merumuskan polanya sendiri dan mesin menjalankannya di SELURUH universe likuid."""
    _stub_universe()
    out = analyst._screen_market(filter="rsi14<30 and vol_vs_avg>2")
    assert "AAAA" in out, out
    assert "BBBB" not in out, "saham yang tak memenuhi syarat ikut lolos"
    assert "CCCC" not in out, "saham ILIKUID lolos — hasil screening jadi tak bisa dieksekusi"
    assert "rsi14=24" in out, "alasan kecocokan tak ditampilkan: " + out


def test_screen_market_kosong_jujur():
    _stub_universe()
    out = analyst._screen_market(filter="rsi14<5")
    assert "0 saham" in out, out


def test_screen_market_menolak_input_berbahaya():
    """Trust boundary: teks dari LLM tak boleh menyentuh SQL / eval."""
    _stub_universe()
    for jahat in ("rsi14 < 30; DROP TABLE quotes",
                  "__import__('os').system('rm -rf /')",
                  "1=1 OR ticker LIKE '%'",
                  "harga_rahasia>1"):
        out = analyst._screen_market(filter=jahat)
        assert out.startswith("("), f"input berbahaya diterima: {jahat!r} -> {out}"


def test_screen_market_batas_syarat():
    _stub_universe()
    out = analyst._screen_market(filter="rsi14<90 and vol_vs_avg>0 and momentum_10d>-99 "
                                        "and above_sma20>-1 and price>0 and change_pct>-99")
    assert "maksimum 5 syarat" in out, out


def test_tool_baru_terdaftar():
    """Skema tool benar-benar dikirim ke model — tanpa ini agen tak tahu tool-nya ada."""
    specs, impls = analyst._tool_specs("BBCA")
    nama = {s["function"]["name"] for s in specs}
    assert {"screen_market", "track_record"} <= nama, nama
    assert nama == set(impls), "skema dan implementasi tool tidak sinkron"


def test_track_record_pakai_papan_skor_nyata():
    repo.factor_scoreboard = lambda min_n=3: [
        {"factor": "asing_beli", "n": 40, "win": 24, "win_rate": 60.0},
        {"factor": "book_bid_heavy", "n": 12, "win": 5, "win_rate": 41.7},
    ]
    assert "60.0% menang dari 40" in analyst._track_record(factor="asing_beli")
    assert "book_bid_heavy" in analyst._track_record()          # kosong = seluruh papan
    assert "belum punya rekam jejak" in analyst._track_record(factor="tak_ada")


# ---------------------------------------------------------------- peredam log
def test_log_fail_meredam_pengulangan():
    """11.518 baris identik/7 hari adalah sebab panel log 'kosong dari agen'. Yang PERTAMA
    tetap utuh; sisanya dihitung, bukan dibuang diam-diam."""
    ditulis = []
    repo.log = lambda agent, phase, msg, **k: ditulis.append(msg)
    llm._dedupe.clear()
    config.LOG_DEDUPE_SEC = 300
    for _ in range(50):
        llm._log_fail("groq|oss|AuthenticationError", "groq/oss gagal: 401")
    assert len(ditulis) == 1, f"peredam bocor: {len(ditulis)} baris"

    llm._dedupe["groq|oss|AuthenticationError"][0] = 0   # jendela lewat → ringkasan muncul
    llm._log_fail("groq|oss|AuthenticationError", "groq/oss gagal: 401")
    assert "+49 kejadian identik" in ditulis[-1], ditulis[-1]

    llm._log_fail("mistral|large|NotFound", "mistral/large gagal: 403")
    assert len(ditulis) == 3, "error dari provider LAIN ikut teredam"


def test_log_fail_bisa_dimatikan():
    ditulis = []
    repo.log = lambda agent, phase, msg, **k: ditulis.append(msg)
    llm._dedupe.clear()
    config.LOG_DEDUPE_SEC = 0
    for _ in range(5):
        llm._log_fail("k", "pesan")
    assert len(ditulis) == 5, "LOG_DEDUPE_SEC=0 harus mengembalikan perilaku lama"


# ---------------------------------------------------------------- dewan bersuara
def test_dewan_mencatat_suaranya():
    """Dulu council.py tak punya SATU pun repo.log — dewan berdebat lalu hilang tanpa jejak."""
    dicatat = []
    repo.log = lambda agent, phase, msg, **k: dicatat.append((agent, k.get("level", "info"), msg))
    council.llm.chat_provider = lambda p, m, msgs, **k: {
        "json": {"direction": "UP", "conviction": 70, "alasan": "momentum"}}
    council.config.COUNCIL_MEMBERS = [("teknikal", "mistral", "x"), ("kontrarian", "nara", "y")]
    council.config.PROVIDER_KEY = {"mistral": "k", "nara": "k"}
    out = council.discuss("BBCA", "tesis apa pun")
    assert "UP" in out
    assert any(a == "council" for a, _, _ in dicatat), "dewan masih bisu di log"


def test_dewan_kosong_dicatat_sebagai_error():
    dicatat = []
    repo.log = lambda agent, phase, msg, **k: dicatat.append((agent, k.get("level", "info"), msg))

    def _mati(*a, **k):
        raise RuntimeError("401 Invalid API Key")

    council.llm.chat_provider = _mati
    council.config.COUNCIL_MEMBERS = [("teknikal", "mistral", "x")]
    council.config.PROVIDER_KEY = {"mistral": "k"}
    assert council.discuss("BBCA", "tesis") == ""
    lv = [(a, l) for a, l, _ in dicatat]
    assert ("council", "warn") in lv, "kursi mati tetap tak terlihat"
    assert ("council", "error") in lv, "dewan kosong total tak dilaporkan"


# ---------------------------------------------------------------- tanpa fallback
def _stub_engine(orch):
    repo.get_quote = lambda t: {"ticker": t, "price": 100.0, "volume": 5e6, "features_json": "{}"}
    repo.get_position = lambda t: None
    repo.get_cash = lambda: 1e7
    repo.log = lambda *a, **k: None
    orch.config.USE_LLM = True
    orch.config.USE_LANGGRAPH = False
    orch.analyst.analyze_cached = _raise_llm
    orch.heuristic.decide = lambda *a, **k: {"direction": "UP", "probability": 60.0,
                                             "action": "BUY", "horizon_days": 3,
                                             "_dari": "heuristik"}


def _raise_llm(*a, **k):
    raise RuntimeError("semua provider gagal — 401 | 402 | 403")


def test_strict_melewati_ticker_bukan_menyamar_heuristik():
    """Inti permintaan 'tanpa fallback': LLM tumbang -> TIDAK ADA prediksi, bukan skor
    teknikal yang masuk papan skor dengan nama agen."""
    from app.agents import orchestrator as orch
    _stub_engine(orch)
    orch.config.LLM_STRICT = True
    assert orch._decide_for("BBCA") is None


def test_strict_bisa_dimatikan():
    """Kill-switch tetap ada: LLM_STRICT=false -> perilaku lama (heuristik mengisi)."""
    from app.agents import orchestrator as orch
    _stub_engine(orch)
    orch.config.LLM_STRICT = False
    d = orch._decide_for("BBCA")
    assert d and d.get("_dari") == "heuristik"


# ---------------------------------------------------------------- filter log
def test_recent_logs_bisa_disaring():
    dipanggil = {}

    class _Conn:
        def execute(self, sql, args):
            dipanggil["sql"], dipanggil["args"] = sql, list(args)
            return type("R", (), {"fetchall": lambda self: []})()

    repo.get_conn = lambda: _Conn()
    repo.recent_logs(50, agent="council", level="warn")
    assert "agent = ?" in dipanggil["sql"] and "level = ?" in dipanggil["sql"]
    assert dipanggil["args"] == ["council", "warn", 50]
    repo.recent_logs(99)
    assert "WHERE" not in dipanggil["sql"], "tanpa filter tak boleh ada WHERE"


def test_recent_logs_batas_limit():
    ditangkap = {}

    class _Conn:
        def execute(self, sql, args):
            ditangkap["args"] = list(args)
            return type("R", (), {"fetchall": lambda self: []})()

    repo.get_conn = lambda: _Conn()
    repo.recent_logs(999999)
    assert ditangkap["args"][-1] == 1000, "limit tak dibatasi — bisa menyedot seluruh tabel"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------- jalur antre provider
def test_throttle_terpisah_per_kunci():
    """nara/nara_free/nara_smart = TIGA kunci dgn TIGA kuota. Dulu antreannya dikunci pada
    base_url yang sama, jadi analis (nara_smart, ~42s) menahan kursi dewan (nara) sampai
    habis waktu — 4 dari 7 ticker kehilangan kursi kontrarian di produksi."""
    import config as _c
    assert (_c.PROVIDER_BASE["nara"] == _c.PROVIDER_BASE["nara_smart"]
            == _c.PROVIDER_BASE["nara_free"]), "prasyarat test berubah: base_url tak lagi sama"

    dipakai = []
    llm._rl_slots.clear()
    llm._throttle = lambda key: dipakai.append(key)

    class _Cli:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("berhenti setelah throttle")

    for prov in ("nara", "nara_smart", "nara_free"):
        try:
            llm._create(_Cli(), prov, retries=0, model="m", messages=[])
        except RuntimeError:
            pass
    assert dipakai == ["nara", "nara_smart", "nara_free"], dipakai
    assert len(set(dipakai)) == 3, "tiga kunci masih berbagi satu jalur antre"
