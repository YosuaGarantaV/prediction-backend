"""Agent SENTIMEN — anggota inti MAS ("berbasis analisis sentimen").
Dua lapis hemat token:
  1. LEXICON (0 token, selalu bisa): agregasi `impact` berita emiten yang SUDAH diskor
     saat ingest (news._sentiment) → arah + keyakinan deterministik.
  2. LLM (kondisional): dinilai HANYA bila ada berita emiten lebih segar dari SENT_FRESH_H
     dan kunci berita berubah sejak penilaian terakhir (memo per-ticker, pola memo.py).
Suaranya dipakai dua arah: council_vote() → suara dewan (reuse cache, 0 token marginal),
note() → catatan untuk prompt CTO / transcript graf."""
from __future__ import annotations

import threading

import config
from app import repo
from app.agents import llm

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}   # ticker -> {"res": dict, "key": (count, max_id)}

_SYS = ("Kamu analis SENTIMEN pasar dewan saham IDX. Nilai HANYA nada berita & narasi pasar "
        "terhadap saham ini (bukan teknikal/valuasi): tone headline, kekuatan katalis, "
        "framing positif/negatif. Balas HANYA JSON: "
        '{"direction":"UP|DOWN|FLAT","conviction":0-100,"alasan":"<=15 kata"}')


def _own_news(ticker: str, max_age_hours: float) -> list[dict]:
    """Berita KHUSUS emiten (bukan makro global) dalam jendela."""
    rows = repo.news_for_ticker(ticker, limit=8, max_age_hours=max_age_hours)
    return [r for r in rows if ticker in (r.get("tickers") or "")]


def _clamp_conv(v, default=50) -> int:
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return default


def _lexicon(ticker: str) -> dict:
    """Lapis deterministik: jumlah impact berita emiten 48 jam → arah/keyakinan. 0 token."""
    own = _own_news(ticker, 48)
    skor = sum(int(r.get("impact") or 0) for r in own)
    if not own:
        return {"direction": "FLAT", "skor": 0, "conviction": 50,
                "alasan": "tak ada berita emiten — sentimen netral", "source": "lexicon"}
    arah = "UP" if skor >= 2 else "DOWN" if skor <= -2 else "FLAT"
    return {"direction": arah, "skor": skor,
            "conviction": min(50 + 8 * abs(skor), 75),   # cap: lexicon jangan sok yakin
            "alasan": f"{len(own)} berita emiten, total impact {skor:+d}",
            "source": "lexicon"}


def _llm_assess(ticker: str, rows: list[dict]) -> dict:
    lines = [f"- [{int(r.get('impact') or 0):+d}] {r.get('title', '')[:120]}" for r in rows[:6]]
    msg = [{"role": "system", "content": _SYS},
           {"role": "user", "content": f"Saham {ticker}. Berita terbaru:\n" + "\n".join(lines)
                                       + "\n\nSentimen pasar terhadap saham ini?"}]
    out = llm.chat_chain(config.SENTIMENT_CHAIN, msg, want_json=True,
                         temperature=0.4, max_tokens=200, timeout=15.0)
    j = out.get("json") or {}
    d = str(j.get("direction", "FLAT")).upper()
    return {"direction": d if d in {"UP", "DOWN", "FLAT"} else "FLAT",
            "skor": None, "conviction": _clamp_conv(j.get("conviction")),
            "alasan": str(j.get("alasan", ""))[:80],
            "source": f"llm/{out.get('provider', '?')}"}


def assess(ticker: str) -> dict:
    """Penilaian sentimen ticker (cache per kunci berita — panggilan ulang gratis)."""
    if not config.SENTIMENT_AGENT:
        return {}
    key = repo.ticker_news_key(ticker)
    with _LOCK:
        c = _CACHE.get(ticker)
    if c and c["key"] == key:
        return c["res"]
    res = None
    if key[0]:                                   # ada berita emiten di jendela 72 jam
        fresh = _own_news(ticker, config.SENT_FRESH_H)
        if fresh:                                # segar → layak 1 panggilan LLM kecil
            try:
                res = _llm_assess(ticker, fresh)
            except Exception as e:  # noqa: BLE001 — LLM gagal → lexicon tetap bicara
                repo.log("sentiment", "assess", f"{ticker}: LLM gagal ({e}) → lexicon",
                         level="warn", ticker=ticker)
    if res is None:
        res = _lexicon(ticker)
    with _LOCK:
        _CACHE[ticker] = {"res": res, "key": key}
    repo.log("sentiment", "assess",
             f"{ticker}: {res['direction']} (yakin {res['conviction']}%) [{res['source']}] "
             f"— {res['alasan']}", ticker=ticker)
    return res


def council_vote(ticker: str) -> dict | None:
    """Suara dewan agen sentimen — bentuk sama dgn council._vote (reuse cache, 0 token)."""
    res = assess(ticker)
    if not res:
        return None
    return {"who": f"sentimen({res['source']})", "role": "sentimen",
            "direction": res["direction"], "conviction": res["conviction"],
            "alasan": res["alasan"]}


def note(ticker: str) -> str:
    """Catatan 1 baris untuk prompt CTO / transcript graf. '' bila agen nonaktif."""
    res = assess(ticker)
    if not res:
        return ""
    return (f"AGEN SENTIMEN [{res['source']}]: {res['direction']} "
            f"(yakin {res['conviction']}%) — {res['alasan']}")


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
