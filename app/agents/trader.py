"""Agent TRADER/CTO. Mengubah analisis jadi keputusan terstruktur (JSON).
Mode agen (TRADER_TOOLS): CTO memutuskan SENDIRI kapan konsultasi dewan / cek fundamental
lewat tool — menggantikan trigger skrip `if ragu` di orchestrator."""
from __future__ import annotations

import json

import config
from app import repo, tick
from app import eval as _eval
from app.agents import llm, prompts


def should_debate(d: dict, rnd: int) -> bool:
    """Layak lanjut debat Analyst<->CTO? SUMBER TUNGGAL (router graf & while-loop manual).
    DEBATE_ONLY_ACTIONABLE: rebuttal (~3.8k token/putaran) hanya bila ada yang dipertaruhkan
    — aksi nyata (BUY/SELL) atau keyakinan >=60; disagreement low-stakes (HOLD lemah) stop."""
    if d.get("agree", True) or rnd >= config.DEBATE_ROUNDS:
        return False
    return (not config.DEBATE_ONLY_ACTIONABLE
            or d.get("action") in ("BUY", "SELL")
            or (d.get("probability") or 0) >= 60)


def _cto_tools(ticker: str, analyst_view: str):
    """Skema + impl tool CTO. Ticker/tesis di-bind via closure; tool tanpa argumen."""
    from app.agents import council
    from app.data import fundamentals
    used = {"council": 0}

    def _consult(**_):
        used["council"] += 1
        notes = council.discuss(ticker, analyst_view)
        return notes or "(dewan tidak tersedia / tak ada suara)"

    impls = {
        "consult_council": _consult,
        "get_fundamentals": lambda **_: fundamentals.fundamentals_brief(ticker),
    }
    specs = [
        {"type": "function", "function": {
            "name": "consult_council",
            "description": ("Minta VOTE arah dari dewan trader multi-model (second opinion "
                            "independen). Panggil bila kamu RAGU arah, keyakinanmu di zona "
                            "abu-abu (55-68%), atau hendak BUY. Jangan panggil bila sudah yakin."),
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"type": "function", "function": {
            "name": "get_fundamentals",
            "description": ("Fundamental & status MSCI saham ini (PER, PBV, ROE, dll) — untuk "
                            "fact-check klaim analis. Panggil HANYA bila keputusan bergantung "
                            "pada valuasi/fundamental."),
            "parameters": {"type": "object", "properties": {}, "required": []}}},
    ]
    return specs, impls, used


def _decide_with_tools(ticker: str, messages: list[dict], analyst_view: str) -> dict:
    """Jalur AGEN: CTO + tool. Lempar bila semua provider gagal (caller fallback JSON biasa)."""
    specs, impls, used = _cto_tools(ticker, analyst_view)
    # timeout 30s (naik dari 20s, 11 Agt): primary pindah dari nemotron-3-ultra (mati) ke
    # deepseek-v4-flash-free — diukur di VM live n=8: 8/8 JSON valid, median 13,7s, maks 19,7s.
    # Batas 20s memotong ekor yang sebenarnya MENJAWAB (1 dari 5 percobaan ke-cut di 20,5s).
    # Biaya: 5 tiker/siklus x ~2 ronde x ~14s = ~140s per siklus 15 menit — masih longgar.
    out = llm.chat_chain_tools(config.TRADER_TOOL_CHAIN, messages, specs, impls,
                               max_rounds=config.TOOL_MAX_ROUNDS, want_json=True,
                               temperature=0.6, max_tokens=1200, timeout=30.0)
    out["_council_calls"] = used["council"]
    return out


def _text(v) -> str:
    """Paksa field LLM jadi STRING TERBACA. Sebagian model balas critique/reasoning sbg
    objek/list → dulu di-json.dumps mentah ('{"setuju": ...}' tampil di transkrip, 11% kritik
    — audit 2026-07-07); kini di-unwrap jadi kalimat. Tetap satu chokepoint via _sanitize
    (tanpa ini slice critique[:1500] meledak → trader gagal → jatuh ke heuristik)."""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_text(x)}" for k, x in v.items())
    if isinstance(v, list):
        return "; ".join(_text(x) for x in v)
    return json.dumps(v, ensure_ascii=False)


def decide(ticker: str, quote: dict, analyst_view: str,
           position: dict | None, cash: float, council_notes: str = "",
           horizon: int | None = None, sentiment_note: str = "") -> dict:
    """CTO: ubah analisis (+ pendapat dewan) jadi keputusan JSON. Lempar kalau SEMUA provider gagal.
    `horizon` = mandat pengguna (tombol 1/3/5 hari) — dipaksa juga pasca-sanitize.
    `sentiment_note` TERPISAH dari council_notes: catatan agen sentimen tidak boleh
    mematikan gate jalur tool di bawah (yang memeriksa council_notes)."""
    user = prompts.trader_user_prompt(ticker, quote, analyst_view, position, cash, horizon=horizon)
    if sentiment_note:
        user += "\n\n" + sentiment_note
    if council_notes:
        user += "\n\n" + council_notes
    messages = [
        {"role": "system", "content": prompts.trader_system_prompt()},  # RINGKAS: ~10× hemat token CTO
        {"role": "user", "content": user},
    ]
    out = None
    agent_tools = None
    # Jalur tool DILEWATI bila council_notes sudah ada (re-decide backstop: suara dewan sudah
    # di prompt — jangan konsultasi dobel via tool).
    if config.TRADER_TOOLS and not council_notes:
        try:
            out = _decide_with_tools(ticker, messages, analyst_view)
            agent_tools = {"tool_calls": out.get("tool_calls", 0),
                           "council_calls": out.get("_council_calls", 0),
                           "trace": out.get("trace") or []}
        except Exception as e:  # noqa: BLE001 — jalur tool gagal → fallback JSON biasa (andal)
            repo.log("trader", "decide", f"{ticker}: jalur tool CTO gagal ({e}) → JSON biasa",
                     level="warn", ticker=ticker)
    if out is None:
        out = llm.ask_trader_json(messages)  # rantai fallback CTO (github→minimax→groq→…)
    decision = out.get("json")
    if not decision:
        raise ValueError(f"Trader tidak mengembalikan JSON valid untuk {ticker}")
    decision = _sanitize(ticker, decision, quote)
    if horizon:
        decision["horizon_days"] = horizon  # mandat pengguna menang atas pilihan LLM
    # KALIBRASI klaim LLM ke realisasi historis — dulu HANYA jalur heuristik yang dikalibrasi,
    # klaim trader lolos mentah (data 7 hari: UP LLM 62-67% cuma menang 1/8). Layer yang sama
    # dgn heuristik: shrink per-arah + cap illikuid (Aturan 71).
    try:
        from app.agents import skill  # lazy (hindari circular saat backtest)
        raw = decision["probability"]
        decision["probability"] = skill.calibrated_probability(decision["direction"], raw)
        turnover = (quote.get("price") or 0.0) * (quote.get("volume") or 0.0)
        if turnover < 1e9 and decision["probability"] > 65.0:
            decision["probability"] = 65.0
        if abs(decision["probability"] - raw) >= 1:
            decision["key_factors"] = list(decision.get("key_factors") or []) + [
                f"keyakinan LLM dikalibrasi ke data nyata ({raw:.0f}%→{decision['probability']:.0f}%)"]
    except Exception:  # noqa: BLE001
        pass
    cto = out.get("provider", "?")
    # ATRIBUSI MODEL per keputusan (kritik pembimbing #4: komposisi model berubah di tengah
    # periode penelitian, dan sampai 2026-09-06 model yang memutus hanya bisa ditebak dari
    # stempel waktu agent_logs). Disimpan ke factors_json di _commit → analisis stabilitas
    # treatment bisa dihitung langsung dari baris prediksi.
    decision["_cto_model"] = f"{cto}/{out.get('model', '?')}"
    tool_txt = ""
    if agent_tools:
        decision["_agent_tools"] = agent_tools  # dipakai orchestrator utk transcript (tak disimpan)
        jejak = ", ".join(t["tool"] for t in agent_tools["trace"]) or "-"
        tool_txt = (f" [agen: {agent_tools['tool_calls']} tool ({jejak}), "
                    f"dewan {agent_tools['council_calls']}x]")
    repo.log("trader", "decide",
             f"{ticker}: {decision['direction']} {decision['probability']}% "
             f"/{decision['horizon_days']}h -> {decision['action']} (CTO: {cto}){tool_txt}",
             ticker=ticker, payload=decision)
    return decision


def _sanitize(ticker: str, d: dict, quote: dict) -> dict:
    price = quote.get("price") or 0.0
    def num(key, default, lo=None, hi=None):
        try:
            v = float(d.get(key, default))
        except (TypeError, ValueError):
            v = default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    direction = str(d.get("direction", "FLAT")).upper()
    if direction not in {"UP", "DOWN", "FLAT"}:
        direction = "FLAT"
    action = str(d.get("action", "HOLD")).upper()
    if action not in {"BUY", "SELL", "HOLD"}:
        action = "HOLD"
    expected = num("expected_pct", 0.0, -50, 50)
    # Target DITURUNKAN dari expected_pct — satu sumber kebenaran. Dulu target bebas dari LLM
    # menang, dan keduanya sering bertengkar: AKRA id419 exp 1,0% tapi target 1621 vs entry
    # 1450 = +11,8% (15 baris tak konsisten, audit 2026-07-27). Itu bukan kosmetik: gate BUY &
    # UI membaca expected_pct sedangkan bracket penutupan awal membaca target_price → jarak
    # penutupan bisa 10x lipat dari yang ditampilkan. expected_pct yang dipertahankan karena
    # sudah tervalidasi (num(), -50..50) dan dipakai gerbang keputusan.
    # Target ditarik ke fraksi harga IDX (app/tick.py): harga tak pernah singgah di antara
    # tick, jadi target berdesimal seperti 102,73 menilai sesuatu yang mustahil tersentuh.
    # target_for juga mengembalikan expected_pct yang benar-benar sesuai target itu.
    target, expected_real = tick.target_for(price, expected, direction)
    if target is not None:
        expected = expected_real

    return {
        "ticker": ticker,
        "direction": direction,
        "probability": num("probability", 50, 0, 100),
        # Batas atas = eval.MAX_HORIZON (250 sesi = 1 tahun). Dulu 10, sehingga horizon
        # bulanan yang diminta prompt akan dipangkas diam-diam jadi 10.
        "horizon_days": int(num("horizon_days", 3, 1, _eval.MAX_HORIZON)),
        "expected_pct": round(expected, 2),
        "target_price": round(float(target), 2) if target else None,
        "entry_price": price,
        "action": action,
        "size_pct": num("size_pct", 0, 0, 15),
        "key_factors": d.get("key_factors") or [],
        "critique": _text(d.get("critique", "")),
        "reasoning": _text(d.get("reasoning", "")),
        "term": str(d.get("term", "pendek")).lower(),  # pendek | panjang | keduanya
        "agree": bool(d.get("agree", True)),  # False => analis perlu membantah lagi
    }
