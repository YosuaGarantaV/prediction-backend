"""Lengan BAYANGAN: ablasi tiga lengan di atas populasi yang IDENTIK.

Kenapa modul ini ada (kritik pembimbing 2026-09-06). Bab IV v3 mengadu multi-agent (95
taruhan) dengan heuristik (109 taruhan), padahal kedua lengan itu tidak pernah melihat
kandidat yang sama: heuristik hanya dicatat untuk sisa `top_record` yang TIDAK dianalisis
LLM (`run_cycle` langkah 3) dan saat provider mati, jadi ia bertaruh di saham peringkat
lebih rendah, pada tanggal lain, dengan campuran arah lain (38% DOWN vs 86% DOWN). Selisih
win-rate antar-lengan seperti itu tak bisa diatribusikan ke kemampuan model — ia bisa
sekadar mengukur perbedaan populasi.

Di sini TIGA lengan menilai kandidat, tanggal, harga masuk, dan horizon yang sama persis:

    mas   keluaran MENTAH graf multi-agent (sebelum gerbang produksi)
    heur  heuristic.decide pada quote yang sama
    llm1  SATU panggilan LLM, model DIPATOK (`config.SHADOW_LLM`), prompt netral,
          tanpa dewan, tanpa debat, tanpa tool

Tak ada lengan yang menyentuh portofolio: tabel `shadow` murni papan skor. Gerbang produksi
(rezim/tren/bukti/kalibrasi/lengan-terbukti) sengaja TIDAK diterapkan supaya yang diukur
kemampuan ARAH, bukan kebijakan uang — kebijakan uang tetap diukur di jalur `predictions`.

Pemenang tidak diputuskan di sini. Modul ini hanya menyetor bukti; vonis dihitung
`python -m app.agents.shadow` (win-rate per lengan + selisih berpasangan per tanggal).
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config
from app import db, market_calendar as _cal, repo

ARMS = ("mas", "heur", "llm1")

# Prompt NETRAL: tak ada persona CTO, tak ada batasan BUY, tak ada suara kontrarian, tak ada
# panduan kalibrasi. Kritik pembimbing #2: prompt trader produksi menekan BUY, jadi lengan
# LLM tunggal harus bebas dari tekanan itu supaya yang diuji arsitektur, bukan wording.
_SYS = ("Kamu analis saham. Baca data yang diberikan, lalu jawab HANYA JSON: "
        '{"direction":"UP|DOWN|FLAT","probability":<50-95>,"horizon_days":<1|3|5>}. '
        "probability = peluang arah itu benar. Tanpa penjelasan, tanpa teks lain.")


def _expires(horizon: int) -> str:
    """Tanggal jatuh tempo dalam HARI BURSA — definisi yang sama dgn repo.save_prediction."""
    return _cal.add_trading_days(datetime.now(repo._WIB).date(), max(1, horizon)).isoformat()


def _save(batch: str, arm: str, ticker: str, d: dict, entry: float, meta: dict) -> None:
    h = int(d.get("horizon_days") or 3)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO shadow(batch, arm, ts, ticker, direction, probability, horizon_days, "
            "entry_price, expires_at, status, meta_json) VALUES (?,?,?,?,?,?,?,?,?, 'open', ?)",
            (batch, arm, repo.now_iso(), ticker, d["direction"],
             float(d.get("probability") or 50.0), h, entry, _expires(h), json.dumps(meta)))


def _llm1(ticker: str, quote: dict) -> dict | None:
    """Satu panggilan, satu model DIPATOK. Gagal → lengan ini KOSONG untuk batch ini.

    Sengaja tanpa rantai fallback: rantai itulah yang membuat treatment di Bab IV tidak
    stabil (17 model / 12 provider, komposisi berubah di tengah eksperimen). Lebih baik
    lubang jujur di data daripada baris yang diam-diam ditulis model lain.
    """
    if not config.SHADOW_LLM:
        return None
    from app.agents import llm, prompts
    provider, _, model = config.SHADOW_LLM.partition(":")
    out = llm.chat_provider(
        provider, model,
        [{"role": "system", "content": _SYS},
         {"role": "user", "content": prompts.analyst_user_prompt(ticker, quote)}],
        want_json=True, temperature=0.6, max_tokens=200, timeout=45.0, retries=0)
    j = out.get("json") or {}
    d = str(j.get("direction", "")).upper()
    if d not in ("UP", "DOWN", "FLAT"):
        return None
    return {"direction": d,
            "probability": max(50.0, min(95.0, float(j.get("probability") or 50))),
            "horizon_days": int(j.get("horizon_days") or 3)}


def record(ticker: str, decision: dict) -> int:
    """Catat ketiga lengan untuk satu kandidat. Return jumlah lengan yang tercatat.

    `decision` = keluaran MENTAH `_decide_for` (dipanggil SEBELUM `_commit` menerapkan
    gerbang, karena `_commit` mengubah dict itu di tempat).
    """
    if not config.SHADOW_ARMS:
        return 0
    quote = repo.get_quote(ticker)
    entry = (quote or {}).get("price")
    if not entry:
        return 0
    batch = f"{ticker}|{repo.now_iso()}"
    n = 0
    for arm, d, meta in _collect(ticker, quote, decision):
        try:
            _save(batch, arm, ticker, d, float(entry), meta)
            n += 1
        except Exception as e:  # noqa: BLE001 — papan skor tak boleh menjatuhkan siklus
            repo.log("shadow", "record", f"{ticker}: lengan {arm} gagal disimpan ({e})",
                     level="warn", ticker=ticker)
    return n


def record_many(items: list[tuple[str, dict]]) -> int:
    """Catat banyak kandidat PARALEL. Lengan llm1 memanggil model bahasa, jadi berurutan ia
    bisa menambah menit ke tiap siklus; papan skor tidak boleh memperlambat produksi."""
    if not config.SHADOW_ARMS or not items:
        return 0
    workers = max(1, min(len(items), config.LLM_PARALLEL))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return sum(ex.map(lambda it: record(*it), items))


def _collect(ticker: str, quote: dict, decision: dict):
    """(arm, keputusan, meta) untuk tiap lengan yang berhasil dihitung."""
    yield "mas", decision, {"cto": decision.get("_cto_model"),
                            "raw_prob": decision.get("raw_prob")}
    from app.agents import heuristic
    try:
        h = heuristic.decide(ticker, quote, repo.get_position(ticker), repo.get_cash())
        yield "heur", h, {"score": h.get("score")}
    except Exception as e:  # noqa: BLE001
        repo.log("shadow", "record", f"{ticker}: lengan heur gagal ({e})", level="warn",
                 ticker=ticker)
    try:
        one = _llm1(ticker, quote)
        if one:
            yield "llm1", one, {"model": config.SHADOW_LLM}
    except Exception as e:  # noqa: BLE001
        repo.log("shadow", "record", f"{ticker}: lengan llm1 gagal ({e})", level="warn",
                 ticker=ticker)


def resolve_due() -> int:
    """Nilai baris bayangan yang horizonnya sudah dijalani. Aturan menang = satu-satunya
    definisi produksi (`orchestrator.is_win`), supaya papan skor ini tak bisa memakai
    ambang berbeda dari jalur `predictions`."""
    from app.agents.orchestrator import is_win
    now = datetime.now(timezone.utc)
    rows = [dict(r) for r in db.get_conn().execute(
        "SELECT id, ts, ticker, direction, horizon_days, entry_price, expires_at "
        "FROM shadow WHERE status='open'")]
    n = 0
    for r in rows:
        if not repo.prediction_is_due(r, now):
            continue
        q = repo.get_quote(r["ticker"])
        if not q or not q.get("price") or not r["entry_price"]:
            continue
        pct = (q["price"] / r["entry_price"] - 1) * 100
        with db.tx() as conn:
            conn.execute("UPDATE shadow SET status='resolved', resolved_at=?, actual_pct=?, "
                         "outcome=? WHERE id=?",
                         (repo.now_iso(), round(pct, 2),
                          "win" if is_win(r["direction"], pct) else "loss", r["id"]))
        n += 1
    if n:
        repo.log("shadow", "resolve", f"{n} baris bayangan dinilai")
    return n


def scoreboard(min_horizon: int = 2) -> dict:
    """Win-rate per lengan HANYA pada batch yang ketiga lengannya lengkap & tuntas —
    tanpa syarat itu perbandingannya kembali jadi populasi berbeda, persis cacat yang
    modul ini perbaiki. Taruhan = arah UP/DOWN, horizon >= `min_horizon`."""
    rows = [dict(r) for r in db.get_conn().execute(
        "SELECT batch, arm, substr(coalesce(resolved_at, ts),1,10) d, direction, "
        "horizon_days, outcome FROM shadow WHERE outcome IN ('win','loss')")]
    per: dict[str, dict[str, dict]] = {}
    for r in rows:
        if r["direction"] in ("UP", "DOWN") and (r["horizon_days"] or 0) >= min_horizon:
            per.setdefault(r["batch"], {})[r["arm"]] = r
    full = [b for b in per.values() if all(a in b for a in ARMS)]
    out = {"batch_lengkap": len(full), "batch_total": len(per), "lengan": {}}
    for arm in ARMS:
        sel = [b[arm] for b in full]
        if sel:
            out["lengan"][arm] = {
                "n": len(sel),
                "hari": len({r["d"] for r in sel}),
                "win_pct": round(sum(r["outcome"] == "win" for r in sel) / len(sel) * 100, 1),
                "down_pct": round(sum(r["direction"] == "DOWN" for r in sel) / len(sel) * 100, 1),
            }
    return out


if __name__ == "__main__":  # python -m app.agents.shadow
    print(json.dumps(scoreboard(), indent=2, ensure_ascii=False))
