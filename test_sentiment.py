"""Cek Agent Sentimen (python test_sentiment.py) — monkeypatch, tanpa DB/API nyata.
Kontrak hemat token: lexicon selalu; LLM HANYA saat berita emiten segar + kunci berubah."""
import config
from app import repo
from app.agents import llm, sentiment

CALLS = []


def _mock_llm(chain, msgs, **kw):
    CALLS.append(chain)
    return {"json": {"direction": "UP", "conviction": 70, "alasan": "katalis positif"},
            "provider": "glm"}


def _setup(news_rows, key=(1, 10)):
    sentiment.clear()
    CALLS.clear()
    repo.news_for_ticker = lambda t, limit=8, max_age_hours=72: news_rows
    repo.ticker_news_key = lambda t, max_age_hours=72: key
    repo.log = lambda *a, **k: None
    llm.chat_chain = _mock_llm
    sentiment.llm.chat_chain = _mock_llm


def test_no_news_lexicon_only():
    _setup([], key=(0, 0))
    res = sentiment.assess("TEST")
    assert res["source"] == "lexicon" and res["direction"] == "FLAT", res
    assert not CALLS, "tak boleh ada panggilan LLM tanpa berita"
    print("lexicon-only tanpa berita ok (0 panggilan LLM)")


def test_stale_news_lexicon():
    # ada berita (key>0) tapi TIDAK segar (news_for_ticker jendela pendek → kosong):
    # assess memanggil news_for_ticker 2x (fresh-check lalu lexicon) — dua-duanya rows sama
    rows = [{"tickers": "", "impact": 3, "title": "makro global"}]  # bukan berita emiten
    _setup(rows, key=(1, 5))
    res = sentiment.assess("TEST")
    assert res["source"] == "lexicon", res
    assert not CALLS, "berita bukan-emiten tak boleh memicu LLM"
    print("berita non-emiten -> lexicon ok")


def test_fresh_news_calls_llm_once_then_memo():
    rows = [{"tickers": "TEST", "impact": 2, "title": "TEST menang tender besar"}]
    _setup(rows, key=(1, 11))
    r1 = sentiment.assess("TEST")
    assert r1["source"].startswith("llm") and r1["direction"] == "UP", r1
    assert len(CALLS) == 1
    r2 = sentiment.assess("TEST")          # kunci sama → cache, tanpa panggilan baru
    assert len(CALLS) == 1 and r2 == r1
    repo.ticker_news_key = lambda t, max_age_hours=72: (2, 12)  # berita baru → kunci berubah
    sentiment.assess("TEST")
    assert len(CALLS) == 2, "kunci berubah harus menilai ulang"
    print("gating LLM + memo per kunci berita ok")


def test_llm_fail_falls_back_lexicon():
    rows = [{"tickers": "TEST", "impact": -3, "title": "TEST digugat"},
            {"tickers": "TEST", "impact": -2, "title": "TEST rugi"}]
    _setup(rows, key=(2, 20))
    def _boom(*a, **k):
        raise RuntimeError("provider mati")
    sentiment.llm.chat_chain = _boom
    res = sentiment.assess("TEST")
    assert res["source"] == "lexicon" and res["direction"] == "DOWN", res
    print("LLM gagal -> lexicon tetap bicara ok")


def test_council_includes_sentimen_vote():
    rows = [{"tickers": "TEST", "impact": 3, "title": "TEST ekspansi"}]
    _setup(rows, key=(1, 30))
    from app.agents import council
    council.llm.chat_provider = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mati"))
    out = council.discuss("TEST", "tesis analis: menarik")
    assert "[sentimen]" in out and "sentimen(" in out, out
    print("suara sentimen hadir di dewan ok (anggota LLM lain gagal pun)")


def test_note_format():
    rows = [{"tickers": "TEST", "impact": 2, "title": "TEST naik"}]
    _setup(rows, key=(1, 40))
    n = sentiment.note("TEST")
    assert n.startswith("AGEN SENTIMEN [") and "UP" in n, n
    print("format note CTO ok")


if __name__ == "__main__":
    test_no_news_lexicon_only()
    test_stale_news_lexicon()
    test_fresh_news_calls_llm_once_then_memo()
    test_llm_fail_falls_back_lexicon()
    test_council_includes_sentimen_vote()
    test_note_format()
    print("ALL OK")
