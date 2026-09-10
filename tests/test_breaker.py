"""Cek circuit breaker per-provider (app.agents.llm): provider kena limit → di-skip sejenak,
chain lompat ke provider berikut tanpa memanggil yang lagi cooldown. Lihat memory saham-idx."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import time

from openai import RateLimitError
from app.agents import llm


def _reset_all():
    with llm._cb_guard:
        llm._cb_until.clear()
        llm._cb_strikes.clear()
        llm._cb_timeouts.clear()


def test_trip_and_cooling():
    _reset_all()
    assert not llm._cooling("groq")
    llm._trip("groq", RateLimitError.__new__(RateLimitError))  # 429 → cooldown
    assert llm._cooling("groq")
    assert "groq" in llm.cooldowns()
    llm._reset("groq")
    assert not llm._cooling("groq")


def test_daily_vs_short_cooldown():
    _reset_all()
    llm._trip("cerebras", Exception("Error 429: Tokens per day limit exceeded"))
    llm._trip("github", Exception("Too many requests"))
    cds = llm.cooldowns()
    assert cds["cerebras"] > llm.config.CB_COOLDOWN + 10, cds   # daily → panjang (2 jam)
    assert cds["github"] <= llm.config.CB_COOLDOWN + 1, cds     # biasa → pendek (90s)


def test_non_capacity_error_no_trip():
    _reset_all()
    llm._trip("mistral", Exception("JSON gagal parse"))         # bukan masalah kapasitas
    assert not llm._cooling("mistral")


def test_timeout_sekali_tidak_men_trip():
    """Timeout adalah jawaban lambat, bukan provider yang menolak. Kalau disamakan dengan
    rate-limit, satu jawaban lambat menyalakan backoff eksponensial dan ticker sesudahnya
    ikut jatuh selama cooldown."""
    _reset_all()
    llm._trip("nara", Exception("Request timed out."))
    assert not llm._cooling("nara"), "timeout pertama tak boleh membuang provider"


def test_timeout_dua_kali_beruntun_men_trip_datar():
    _reset_all()
    llm._trip("nara", Exception("Request timed out."))
    llm._trip("nara", Exception("Request timed out."))
    assert llm._cooling("nara")
    cd = llm.cooldowns()["nara"]
    assert cd <= llm.config.CB_TIMEOUT_COOLDOWN + 1, cd        # datar & pendek
    assert cd < llm.config.CB_COOLDOWN, cd                     # lebih pendek dari jalur limit


def test_timeout_tidak_eskalasi_seperti_rate_limit():
    """Rate-limit naik 90 -> 180 -> ...; timeout TIDAK, supaya provider lambat tak terkubur."""
    _reset_all()
    for _ in range(4):
        llm._trip("nara_smart", Exception("Request timed out."))
    assert llm.cooldowns()["nara_smart"] <= llm.config.CB_TIMEOUT_COOLDOWN + 1
    _reset_all()
    llm._trip("groq", Exception("429 rate limit"))
    llm._trip("groq", Exception("429 rate limit"))
    assert llm.cooldowns()["groq"] > llm.config.CB_COOLDOWN, "429 harus tetap eskalasi"


def test_sukses_memutus_hitungan_timeout():
    _reset_all()
    llm._trip("nara", Exception("Request timed out."))
    llm._reset("nara")                                   # satu jawaban sukses
    llm._trip("nara", Exception("Request timed out."))
    assert not llm._cooling("nara"), "hitungan beruntun harus putus oleh sukses"


def test_chain_skips_cooling_provider():
    _reset_all()
    llm.config.PROVIDER_KEY = {**llm.config.PROVIDER_KEY, "aaa": "k", "bbb": "k"}  # chain butuh key
    calls = []

    def fake_provider(provider, model, messages, **kw):
        calls.append(provider)
        if llm._cooling(provider):
            raise llm._Cooling(provider)
        if provider == "aaa":
            raise RuntimeError("aaa mati")
        return {"content": "ok", "provider": provider, "model": model, "json": {"x": 1}}

    llm.chat_provider = fake_provider
    # aaa cooldown aktif → chain harus lompat ke bbb TANPA benar2 memanggil aaa berulang
    llm._trip("aaa", Exception("too many requests"))
    out = llm.chat_chain([("aaa", "m1"), ("bbb", "m2")], [{"role": "user", "content": "x"}],
                         want_json=True)
    assert out["provider"] == "bbb", out
    assert calls == ["aaa", "bbb"], calls  # aaa dipanggil 1× lalu raise _Cooling → lompat bbb


if __name__ == "__main__":
    for fn in [test_trip_and_cooling, test_daily_vs_short_cooldown,
               test_non_capacity_error_no_trip, test_timeout_sekali_tidak_men_trip,
               test_timeout_dua_kali_beruntun_men_trip_datar,
               test_timeout_tidak_eskalasi_seperti_rate_limit,
               test_sukses_memutus_hitungan_timeout, test_chain_skips_cooling_provider]:
        fn()
        print("OK", fn.__name__)
    print("OK test_breaker: trip/cooling, daily vs pendek, non-kapasitas tak trip, chain lompat.")
