"""Cek dewan multi-provider (python test_council.py) — tanpa jaringan (mock chat_provider)."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import pytest

import config
from app.agents import llm, council


def _berkunci(chain) -> int:
    """Berapa anggota chain yang benar-benar punya kunci di .env."""
    return sum(1 for p, _ in chain if config.PROVIDER_KEY.get(p))


def test_cto_fallback():
    """CTO chain: provider PRIMARY gagal → jatuh ke berikutnya, bukan error.
    Primary = provider TERKONFIGURASI pertama di chain (chat_chain melewati yg tak ada key,
    mis. 'colab' saat Colab mati) — tahan perubahan urutan config & provider lokal off."""
    chain = config.TRADER_CTO_CHAIN
    if not _berkunci(chain):
        pytest.skip("tak ada provider CTO berkunci di .env — isi minimal satu")
    # cermin logika skip chat_chain: provider tanpa key dilewati, jadi "primary" efektif =
    # yang pertama benar-benar dicoba (dulu asumsi chain[0] basi saat primary = colab off).
    primary = next(p for p, _ in chain if config.PROVIDER_KEY.get(p))
    calls = []
    def fake(provider, model, messages, **kw):
        calls.append(provider)
        if provider == primary:
            raise RuntimeError("DEGRADED function cannot be invoked")  # primary mati
        return {"content": "{}", "provider": provider, "model": model,
                "json": {"direction": "UP", "action": "BUY"}}
    llm.chat_provider = fake
    out = llm.chat_chain(chain, [{"role": "user", "content": "x"}], want_json=True)
    assert out["provider"] != primary, out           # primary dilewati
    assert out["json"]["direction"] == "UP"
    assert calls[0] == primary                        # tetap COBA primary (terkonfigurasi) dulu
    print(f"cto fallback ok ({primary} mati -> provider berikut ambil alih)")


def test_discuss_aggregates():
    # Pakai keanggotaan dewan SEBENARNYA (format baru: peran, provider, model). Anggota pertama
    # gagal → dilewati; sisanya tetap diringkas DENGAN PERAN-nya. Anggota 'sentimen' TIDAK
    # vote via LLM: suaranya reuse agen sentimen (di-stub deterministik di sini).
    from app.agents import sentiment
    sentiment.council_vote = lambda t: {"who": "sentimen(lexicon)", "role": "sentimen",
                                        "direction": "FLAT", "conviction": 50, "alasan": "tes"}
    members = [(role, p, m) for role, p, m in config.COUNCIL_MEMBERS
               if config.PROVIDER_KEY.get(p) or role == "sentimen"]
    llm_members = [x for x in members if x[0] != "sentimen"]
    if len(llm_members) < 2:
        pytest.skip("dewan butuh >=2 anggota LLM berkunci — isi kunci provider di .env")
    fail_role, fail_p, _ = llm_members[0]
    seen_roles = []
    def fake(provider, model, messages, **kw):
        seen_roles.append(messages[0]["content"])     # system prompt = lensa peran
        if provider == fail_p:
            raise RuntimeError("timeout")             # 1 anggota gagal → dilewati
        return {"json": {"direction": "UP", "conviction": 70, "alasan": "tes"}}
    llm.chat_provider = fake
    notes = council.discuss("BBCA", "tesis analis xyz")
    assert "PENDAPAT DEWAN" in notes
    assert fail_p not in notes                         # yg gagal tak masuk
    surv = llm_members[1:]
    assert any(role in notes for role, _, _ in surv)   # peran yg sukses muncul di blok
    assert "[sentimen]" in notes                       # suara agen sentimen selalu hadir
    # BUKTI berperan beda: tiap anggota LLM kirim system prompt (lensa) yang BERBEDA
    assert len(set(seen_roles)) == len(llm_members), "tiap anggota harus punya lensa berbeda"
    print("discuss ok (berperan beda, anggota gagal dilewati, sentimen reuse agen)")


def test_dynamic_council_only_when_uncertain():
    """Koordinasi dinamis: dewan HANYA dikonsultasikan saat trader ragu (agree=false /
    keyakinan 55-68 / BUY). Trader yakin → dewan dilewati (hemat + decision-driven)."""
    from app import repo
    from app.agents import orchestrator as orch
    calls = {"council": 0, "trader": 0}
    repo.get_quote = lambda t: {"ticker": t, "price": 100.0, "volume": 5e6,
                                "features_json": "{}"}
    repo.get_position = lambda t: None
    repo.get_cash = lambda: 1e7
    repo.log = lambda *a, **k: None
    orch.news.fetch_ticker_news = lambda t: None
    orch.memo.get_thesis = lambda t, q: "tesis uji"
    orch._sigs = []
    orch.council.discuss = lambda t, v: calls.__setitem__("council", calls["council"] + 1) or "PENDAPAT DEWAN: UP"

    def make_trader(prob, agree=True, action="HOLD"):
        def fake(t, q, v, p, c, council_notes="", horizon=None):
            calls["trader"] += 1
            return {"ticker": t, "direction": "DOWN", "probability": prob, "horizon_days": 3,
                    "expected_pct": -2, "target_price": 98, "entry_price": 100, "action": action,
                    "size_pct": 0, "key_factors": [], "critique": "c", "reasoning": "r",
                    "term": "pendek", "agree": agree}
        return fake

    import config
    config.USE_LLM = True
    config.TRADER_TOOLS = False   # trigger skrip = perilaku mode PIPELINE (pin, jangan ikut .env)
    from app.data import premarket
    premarket.global_brief = lambda: {"score": 0.0}
    # YAKIN (prob 75, agree, HOLD) → dewan TIDAK dipanggil, trader 1x
    orch.trader.decide = make_trader(75)
    calls.update(council=0, trader=0)
    d = orch._decide_for("AAAA")
    assert calls["council"] == 0 and calls["trader"] == 1, calls
    # RAGU (prob 60) → dewan dipanggil + trader putuskan ulang (2x)
    orch.trader.decide = make_trader(60)
    calls.update(council=0, trader=0)
    d = orch._decide_for("AAAA")
    assert calls["council"] == 1 and calls["trader"] == 2, calls
    # MODE AGEN + CTO SUDAH konsultasi via tool → backstop TIDAK dobel panggil dewan
    config.TRADER_TOOLS = True
    base_fake = make_trader(60)
    def cto_patuh(*a, **k):
        d = base_fake(*a, **k)
        d["_agent_tools"] = {"tool_calls": 1, "council_calls": 1}
        return d
    orch.trader.decide = cto_patuh
    calls.update(council=0, trader=0)
    d = orch._decide_for("AAAA")
    assert calls["council"] == 0 and calls["trader"] == 1, calls
    # MODE AGEN + CTO LEWATI norma (ragu tapi 0 konsultasi) → BACKSTOP skrip konsultasi dewan
    orch.trader.decide = make_trader(60)
    calls.update(council=0, trader=0)
    d = orch._decide_for("AAAA")
    assert calls["council"] == 1 and calls["trader"] == 2, calls
    config.TRADER_TOOLS = False


def _eff_primary(chain):
    """Provider TERKONFIGURASI pertama di chain (cermin skip chat_chain). Colab (lokal, sering
    off → tanpa key) dilewati → primary EFEKTIF = cloud pertama yang benar-benar dipakai."""
    return next((p for p, _ in chain if config.PROVIDER_KEY.get(p)), chain[0][0])


def test_role_pins_distinct():
    """Anti role-collapse: pin PRIMARY EFEKTIF tiap PERAN harus provider BERBEDA — kondisi
    normal 'banyak agen' bukan satu model ganti topeng. Pakai primary efektif (skip colab yg
    tak terkonfigurasi) — mencerminkan kondisi jalan sebenarnya. CATATAN DESAIN: kalau Colab
    DINYALAKAN, analis & CTO sama-sama pakai colab (role-collapse) = trade-off sengaja (model
    lokal gratis 0-kuota di atas independensi); saat colab off keduanya divergen (distinct)."""
    pins = {
        "analis": _eff_primary(config.ANALYST_CHAIN),
        "cto": _eff_primary(config.TRADER_CTO_CHAIN),
        "sentimen": _eff_primary(config.SENTIMENT_CHAIN),
        "evaluator": _eff_primary(config.EVALUATOR_CHAIN),
    }
    for role, p, _ in config.COUNCIL_MEMBERS:
        if role != "sentimen":
            pins[f"dewan-{role}"] = p
    assert len(set(pins.values())) == len(pins), f"pin peran tabrakan: {pins}"
    print("pin peran distinct ok:", ", ".join(f"{k}={v}" for k, v in pins.items()))


def test_trader_retry_before_heuristic():
    """CTO gagal total 1x (badai 429) → tunggu breaker pulih (sleep di-mock) → ulang 1x
    sukses. Heuristik TIDAK dipakai — tesis analis tak terbuang (audit 2026-07-07: 24%
    percakapan kehilangan CTO)."""
    import time as _time
    from app import repo
    from app.agents import orchestrator as orch
    config.USE_LLM = True
    config.TRADER_TOOLS = False
    old_lg = config.USE_LANGGRAPH
    config.USE_LANGGRAPH = False          # uji jalur manual (rumah retry)
    repo.get_quote = lambda t: {"ticker": t, "price": 100.0, "volume": 5e6,
                                "features_json": "{}"}
    repo.get_position = lambda t: None
    repo.get_cash = lambda: 1e7
    repo.log = lambda *a, **k: None
    orch.news.fetch_ticker_news = lambda t: None
    orch.memo.get_thesis = lambda t, q: "tesis uji"
    orch.council.discuss = lambda t, v: ""
    from app.data import premarket
    premarket.global_brief = lambda: {"score": 0.0}
    real_sleep, slept = _time.sleep, []
    orch.time.sleep = lambda s: slept.append(s)
    calls = {"n": 0}
    def flaky(t, q, v, p, c, council_notes="", horizon=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 semua provider")
        return {"ticker": t, "direction": "DOWN", "probability": 75.0, "horizon_days": 3,
                "expected_pct": -2, "target_price": 98, "entry_price": 100, "action": "HOLD",
                "size_pct": 0, "key_factors": [], "critique": "c", "reasoning": "r",
                "term": "pendek", "agree": True}
    orch.trader.decide = flaky
    try:
        d = orch._decide_for("AAAA")
    finally:
        orch.time.sleep = real_sleep
        config.USE_LANGGRAPH = old_lg
    assert calls["n"] == 2 and slept == [config.TRADER_RETRY_WAIT_S], (calls, slept)
    assert "aturan teknikal" not in (d.get("_analyst_view") or ""), "jatuh ke heuristik!"
    assert d["direction"] == "DOWN"
    print("retry CTO ok: gagal 1x -> tunggu -> sukses, tanpa heuristik")


if __name__ == "__main__":
    test_cto_fallback()
    test_discuss_aggregates()
    test_dynamic_council_only_when_uncertain()
    test_role_pins_distinct()
    test_trader_retry_before_heuristic()
    print("ALL OK")
