"""DEWAN BERPERAN: tiap anggota menilai tesis analis dari LENSA berbeda (teknikal murni vs
kontrarian), bukan N voter identik. Jumlah panggilan = jumlah anggota (token setara dewan lama);
yang beda cuma system prompt per peran. Suara lebih independen dari analis (yang sudah mensintesis
semua). CTO tetap penentu akhir. Toleran gagal: anggota error dilewati (ponytail)."""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import config
from app import repo
from app.agents import llm

_VOTE_BASE = ("Nilai SINGKAT & tegas DARI SUDUT PANDANG PERANMU saja. Balas HANYA JSON: "
              '{"direction":"UP|DOWN|FLAT","conviction":0-100,"alasan":"<=15 kata"}')

# Lensa tiap peran — sengaja ORTOGONAL thd analis (yg sudah gabung teknikal+fundamental+makro).
_ROLE_LENS = {
    "teknikal": ("Kamu analis TEKNIKAL MURNI dewan saham IDX. Abaikan narasi & cerita; nilai HANYA "
                 "price action: momentum (RSI/MACD), tren vs SMA, volume, support/resistance."),
    "kontrarian": ("Kamu KONTRARIAN / devil's advocate dewan saham IDX. Tugasmu mencari kenapa tesis "
                   "analis bisa SALAH: risiko tersembunyi, sisi yang diabaikan, tanda overconfidence."),
    "fundamental": ("Kamu analis FUNDAMENTAL dewan saham IDX. Nilai HANYA valuasi (PER/PBV), ROE, "
                    "growth, kesehatan neraca. Abaikan noise teknikal jangka pendek."),
    "makro": ("Kamu strategist MAKRO dewan saham IDX. Nilai dari rezim global, arus asing, rotasi "
              "sektor, arah IHSG — bukan detail satu saham."),
    "sentimen": ("Kamu analis SENTIMEN pasar dewan saham IDX. Nilai HANYA nada berita & narasi "
                 "pasar: tone headline, kekuatan katalis, framing positif/negatif."),
}


def _role_sys(role: str) -> str:
    lens = _ROLE_LENS.get(role, "Kamu anggota dewan trader saham IDX (generalis).")
    return f"{lens}\n{_VOTE_BASE}"


def _vote(role: str, provider: str, model: str, ticker: str, brief: str) -> dict | None:
    msg = [{"role": "system", "content": _role_sys(role)},
           {"role": "user", "content": f"Saham {ticker}. Ringkasan analis:\n{brief[:1800]}\n\n"
                                        f"Arahmu (khusus dari peran {role})?"}]
    try:
        # Batas dari config (COUNCIL_VOTE_TIMEOUT): vote berjalan PARALEL antar kursi, jadi
        # biayanya = kursi TERLAMBAT. 15s dulu memotong kursi yang butuh 12,9s tanpa margin.
        out = llm.chat_provider(provider, model, msg, want_json=True,
                                temperature=0.5, max_tokens=500,
                                timeout=config.COUNCIL_VOTE_TIMEOUT, retries=0)
        j = out.get("json")
        if not j:
            return None
        d = str(j.get("direction", "FLAT")).upper()
        return {"who": f"{role}({provider})", "role": role,
                "direction": d if d in {"UP", "DOWN", "FLAT"} else "FLAT",
                "conviction": j.get("conviction", 50),
                "alasan": str(j.get("alasan", ""))[:80]}
    except Exception as e:  # noqa: BLE001 — kursi ini kosong, TAPI jangan diam-diam
        # Audit 2026-09-02: dewan tak pernah menulis SATU baris pun ke agent_logs, jadi saat
        # anggotanya mati (401/402/429) dewan menyusut tanpa jejak dan panel log terlihat
        # "kosong dari agen". Kursi gagal kini terlihat, lengkap dgn sebabnya.
        repo.log("council", "debate", f"{ticker}: kursi {role} ({provider}/{model}) "
                 f"kosong — {str(e)[:90]}", level="warn", ticker=ticker)
        return None


def discuss(ticker: str, analyst_view: str) -> str:
    """Vote anggota dewan BERPERAN (paralel) → blok teks untuk prompt CTO. '' kalau nonaktif."""
    if not config.USE_COUNCIL or not analyst_view:
        return ""
    # role 'sentimen' TIDAK butuh key provider: suaranya reuse cache agen sentimen
    # (lexicon fallback tanpa LLM), jadi lolos filter walau GLM key kosong.
    members = [(role, p, m) for role, p, m in config.COUNCIL_MEMBERS
               if config.PROVIDER_KEY.get(p) or role == "sentimen"]
    if not members:
        return ""

    def _member_vote(rpm):
        role, p, m = rpm
        if role == "sentimen":
            from app.agents import sentiment  # lazy: hindari import melingkar saat startup
            try:
                return sentiment.council_vote(ticker)
            except Exception:  # noqa: BLE001 — agen sentimen gagal → suaranya dilewati
                return None
        return _vote(role, p, m, ticker, analyst_view)

    votes = []
    with ThreadPoolExecutor(max_workers=len(members)) as ex:
        for v in ex.map(_member_vote, members):
            if v:
                votes.append(v)
    if not votes:
        repo.log("council", "debate", f"{ticker}: dewan TIDAK memberi suara — "
                 f"{len(members)} kursi semuanya gagal", level="error", ticker=ticker)
        return ""
    lines = [f"- [{v['role']}] {v['who']}: {v['direction']} (yakin {v['conviction']}%) — {v['alasan']}"
             for v in votes]
    tally = Counter(v["direction"] for v in votes)
    konsensus = ", ".join(f"{k}:{n}" for k, n in tally.most_common())
    # Suara dewan sekarang MASUK agent_logs: yang dulu hanya hidup di dalam prompt CTO (dan
    # hilang begitu siklus selesai) kini bisa dibaca di panel log & diaudit belakangan.
    repo.log("council", "debate",
             f"{ticker}: {len(votes)}/{len(members)} kursi bersuara → konsensus {konsensus} | "
             + "; ".join(f"{v['role']}={v['direction']} {v['conviction']}%" for v in votes),
             ticker=ticker, payload={"votes": votes, "tally": dict(tally)})
    return ("PENDAPAT DEWAN (tiap anggota dari PERAN berbeda — timbang lensanya, KAMU CTO penentu "
            "akhir):\n" + "\n".join(lines) + f"\nKonsensus suara: {konsensus}")
