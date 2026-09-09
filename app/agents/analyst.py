"""Agent ANALYST. Menghasilkan tesis analisis (jalur inject-all atau jalur tool on-demand).
Mode agen: punya MEMORI sendiri (tool remember → dibaca lagi di analisis berikutnya) dan
ATENSI sendiri (tool suggest_ticker → antrean kandidat siklus, di-drain run_cycle)."""
from __future__ import annotations

import re
import threading

import config
from app import repo
from app.agents import llm, prompts

# Antrean saran agen (thread-safe: _decide_for jalan paralel). Cap keras: anti-runaway token.
_SUGGEST_LOCK = threading.Lock()
_SUGGEST: list[tuple[str, str]] = []


def drain_suggestions() -> list[tuple[str, str]]:
    """Ambil & kosongkan antrean saran (dipanggil run_cycle sekali per siklus)."""
    global _SUGGEST
    with _SUGGEST_LOCK:
        out, _SUGGEST = _SUGGEST, []
    return out


def _remember(ticker: str, note) -> str:
    """Simpan catatan agen (validasi input LLM: trust boundary — cap panjang, buang sampah)."""
    note = " ".join(str(note or "").split())[:300]
    if len(note) < 10:
        return "(catatan terlalu pendek — tidak disimpan)"
    repo.add_lesson("agent-note", note, ticker=ticker)
    return "(dicatat — akan kamu lihat lagi saat menganalisis saham ini)"


def _suggest(current: str, t, reason) -> str:
    t = str(t or "").upper().strip()
    if not re.fullmatch(r"[A-Z0-9]{2,6}", t) or t == current:
        return f"(ticker tidak valid untuk disarankan: {t!r})"
    if not repo.get_quote(t):
        return f"({t} tidak ada di universe DB — saran tak bisa diproses)"
    with _SUGGEST_LOCK:
        if any(x[0] == t for x in _SUGGEST) or len(_SUGGEST) >= 8:
            return f"({t} sudah dalam antrean / antrean penuh)"
        _SUGGEST.append((t, str(reason or "")[:120]))
    return f"({t} masuk antrean analisis siklus ini)"


def _peers_brief(ticker: str) -> str:
    """Saham SEBANDING (sektor sama, industri diprioritaskan) + gerak hari ini — dari DB lokal
    (0 panggilan API). Padat: 1 baris/peer supaya hemat token di loop agen."""
    conn = repo.get_conn()
    row = conn.execute("SELECT sector, industry FROM fundamentals WHERE ticker=?",
                       (ticker,)).fetchone()
    if not row or not row["sector"]:
        return "(sektor saham ini belum ada di DB — tak bisa cari peers)"
    peers = conn.execute(
        "SELECT f.ticker, f.industry, f.per, f.pbv, q.change_pct FROM fundamentals f "
        "JOIN quotes q ON q.ticker=f.ticker "
        "WHERE f.sector=? AND f.ticker!=? "
        "ORDER BY (f.industry=?) DESC, q.volume*q.price DESC LIMIT 8",
        (row["sector"], ticker, row["industry"])).fetchall()
    if not peers:
        return f"(tak ada peer {row['sector']} di DB)"
    lines = [f"- {p['ticker']} [{p['industry']}]: {(p['change_pct'] or 0):+.1f}% hari ini, "
             f"PER {p['per'] if p['per'] else '-'}, PBV {p['pbv'] if p['pbv'] else '-'}"
             for p in peers]
    return f"PEERS {ticker} (sektor {row['sector']}):\n" + "\n".join(lines)


def _ticker_snapshot(t) -> str:
    """Snapshot padat saham APA PUN dari DB (harga + teknikal inti). Validasi input dari LLM
    (trust boundary): hanya kode ticker alfanumerik pendek yang diterima."""
    import json as _json
    import re
    t = str(t or "").upper().strip()
    if not re.fullmatch(r"[A-Z0-9]{2,6}", t):
        return f"(ticker tidak valid: {t!r})"
    q = repo.get_quote(t)
    if not q:
        return f"({t}: tidak ada di DB — hanya saham IDX ter-scan yang tersedia)"
    feats = _json.loads(q.get("features_json") or "{}")
    core = {k: feats.get(k) for k in ("rsi14", "macd_hist", "above_sma20", "vol_vs_avg",
                                      "momentum_10d", "cum_change_3d", "pct_from_52w_high")
            if feats.get(k) is not None}
    return (f"{t}: harga {q['price']} ({(q['change_pct'] or 0):+.1f}% hari ini), "
            f"teknikal inti: {_json.dumps(core, ensure_ascii=False)}")


def _search_news(query) -> str:
    """Cari arsip berita OSINT (search engine FTS5) → ringkas padat utk loop agen.
    Validasi input dari LLM (trust boundary): query dipangkas, kosong ditolak di repo."""
    query = str(query or "").strip()[:120]
    if len(query) < 2:
        return "(query terlalu pendek)"
    rows = repo.search_news(query, limit=6, max_age_days=45)
    if not rows:
        return f"(tak ada berita cocok untuk '{query}')"
    lines = [f"- [{r['ts'][:10]}|impact {r.get('impact', 0):+d}|{r.get('tickers') or '-'}] "
             f"{(r.get('title') or '')[:120]}" for r in rows]
    return f"HASIL CARI '{query}' ({len(rows)}):\n" + "\n".join(lines)


# --- Penyaring pasar: agen merumuskan POLA-nya sendiri, mesin menjalankannya ---
# Fitur teknikal yang boleh dipakai di filter. Daftar TERTUTUP = trust boundary: teks dari LLM
# tak pernah menyentuh SQL maupun eval(); ia cuma dicocokkan ke nama di daftar ini.
_SCREEN_FIELDS = {
    "rsi14", "macd_hist", "macd_pos", "above_sma20", "vol_vs_avg", "momentum_10d",
    "cum_change_3d", "change_pct", "consec_up", "consec_down", "volatility_20d",
    "bollinger_pct_b", "dist_to_support", "dist_to_resistance", "near_support",
    "near_resistance", "pct_from_52w_high", "pct_from_52w_low", "bull_divergence",
    "bear_divergence", "days_listed", "ara_days_5", "arb_days_5", "price",
}
_CLAUSE = re.compile(r"^\s*([a-z0-9_]+)\s*(>=|<=|>|<|==|=)\s*(-?\d+(?:\.\d+)?)\s*$", re.I)


def _screen_market(filter="", **_) -> str:
    """Sapu SELURUH universe likuid dengan syarat teknikal bebas rumusan agen.

    Ini yang membuat agen bisa menemukan pola BARU, bukan cuma membaca satu saham: ia menulis
    hipotesis ('RSI di bawah 30 tapi arus volume 2x') lalu langsung melihat siapa saja yang
    memenuhinya hari ini. Tanpa tool ini agen hanya bisa memeriksa ticker yang sudah disodorkan
    skrip scan — sekadar pembaca, bukan pencari.
    """
    raw = str(filter or "").strip()[:200]
    if not raw:
        return "(filter kosong — contoh: 'rsi14<30 and vol_vs_avg>2 and change_pct>0')"
    clauses = []
    for part in re.split(r"\s+and\s+|\s*,\s*|\s*&&\s*", raw, flags=re.I):
        if not part.strip():
            continue
        m = _CLAUSE.match(part)
        if not m:
            return (f"(syarat tak dikenal: {part.strip()!r}. Format: <fitur><op><angka>, "
                    f"gabung dengan 'and'. Fitur sah: {', '.join(sorted(_SCREEN_FIELDS))})")
        field, op, num = m.group(1).lower(), m.group(2), float(m.group(3))
        if field not in _SCREEN_FIELDS:
            return (f"(fitur {field!r} tidak ada. Yang tersedia: "
                    f"{', '.join(sorted(_SCREEN_FIELDS))})")
        clauses.append((field, "==" if op == "=" else op, num))
    if len(clauses) > 5:
        return "(maksimum 5 syarat per penyaringan)"

    import json as _json
    import operator
    ops = {">": operator.gt, "<": operator.lt, ">=": operator.ge,
           "<=": operator.le, "==": operator.eq}
    liquid = repo.liquid_tickers()
    hits = []
    for q in repo.all_quotes():
        if q["ticker"] not in liquid:
            continue          # hanya saham yang benar-benar bisa dieksekusi
        try:
            feats = _json.loads(q.get("features_json") or "{}")
        except Exception:  # noqa: BLE001
            continue
        feats["price"] = q.get("price")
        vals = []
        for field, op, num in clauses:
            v = feats.get(field)
            if v is None:
                break
            try:
                if not ops[op](float(v), num):
                    break
            except (TypeError, ValueError):
                break
            vals.append(f"{field}={float(v):g}")
        else:
            hits.append((q["ticker"], q.get("change_pct") or 0.0, ", ".join(vals)))
    if not hits:
        return f"(0 saham likuid memenuhi '{raw}' hari ini — longgarkan syaratnya)"
    hits.sort(key=lambda h: -abs(h[1]))
    shown = hits[:config.SCREEN_MAX_HITS]
    lines = [f"- {t}: {chg:+.1f}% hari ini | {why}" for t, chg, why in shown]
    more = f"\n(+{len(hits) - len(shown)} lagi tak ditampilkan)" if len(hits) > len(shown) else ""
    return f"PENYARINGAN '{raw}' — {len(hits)} dari {len(liquid)} saham likuid:\n" + "\n".join(lines) + more


def _track_record(factor="", **_) -> str:
    """Rekam jejak NYATA tiap faktor struktural: berapa kali fired & berapa persen menang.

    Pasangan wajib screen_market: pola yang baru ditemukan agen jadi bisa ditimbang terhadap
    catatan sistem sendiri, bukan cuma terdengar masuk akal. Sumber = prediksi yang SUDAH
    resolved (repo.factor_scoreboard), jadi tak ada klaim tanpa bukti.
    """
    try:
        board = repo.factor_scoreboard(min_n=3)
    except Exception as e:  # noqa: BLE001
        return f"(papan skor faktor tak terbaca: {e})"
    if not board:
        return "(belum ada faktor dengan minimal 3 prediksi resolved — rekam jejak belum berarti)"
    want = str(factor or "").strip().lower()[:40]
    if want:
        row = next((r for r in board if want in r["factor"].lower()), None)
        if not row:
            return (f"(faktor '{want}' belum punya rekam jejak. Yang ada: "
                    f"{', '.join(r['factor'] for r in board[:12])})")
        return (f"REKAM JEJAK {row['factor']}: {row['win_rate']}% menang dari {row['n']} "
                f"prediksi resolved ({row['win']} menang).")
    lines = [f"- {r['factor']}: {r['win_rate']}% menang (n={r['n']})" for r in board[:12]]
    return "REKAM JEJAK FAKTOR (prediksi resolved):\n" + "\n".join(lines)


def _tool_specs(ticker: str):
    """Skema + impl tool on-demand analis. Ticker utama di-bind lewat closure; check_ticker
    menerima ARGUMEN → agen bebas memeriksa saham sebanding pilihannya sendiri (self-directed)."""
    from app import events
    from app.data import fundamentals
    impls = {
        "get_fundamentals": lambda **_: fundamentals.fundamentals_brief(ticker),
        "get_forward_catalysts": lambda **_: events.events_brief(21),
        "get_sector_peers": lambda **_: _peers_brief(ticker),
        "check_ticker": lambda ticker="", **_: _ticker_snapshot(ticker),
        "search_news": lambda query="", **_: _search_news(query),
        "screen_market": _screen_market,
        "track_record": _track_record,
        "remember": lambda note="", **_: _remember(ticker, note),
        # _cur= binding: param `ticker` dari LLM menshadow ticker closure — jangan tertukar
        "suggest_ticker": lambda ticker="", reason="", _cur=ticker, **_: _suggest(_cur, ticker, reason),
    }
    specs = [
        {"type": "function", "function": {
            "name": "get_fundamentals",
            "description": ("Fundamental & status MSCI saham ini (PER, PBV, ROE, dll). Panggil "
                            "HANYA jika menilai valuasi / kelayakan jangka panjang."),
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"type": "function", "function": {
            "name": "get_forward_catalysts",
            "description": ("Kalender katalis 21 hari ke depan (dividen, RUPS, rilis data). Panggil "
                            "HANYA jika arah bergantung pada agenda ke depan."),
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"type": "function", "function": {
            "name": "get_sector_peers",
            "description": ("Daftar saham SEBANDING (sektor/industri sama) + gerak hari ini & "
                            "valuasinya. Panggil bila perlu konfirmasi apakah gerakan ini "
                            "sektoral (rotasi) atau spesifik saham ini."),
            "parameters": {"type": "object", "properties": {}, "required": []}}},
        {"type": "function", "function": {
            "name": "check_ticker",
            "description": ("Snapshot harga + teknikal inti saham IDX LAIN pilihanmu (mis. peer "
                            "hasil get_sector_peers, atau indeks proxy). Boleh beberapa sekaligus "
                            "dalam satu putaran."),
            "parameters": {"type": "object", "properties": {
                "ticker": {"type": "string", "description": "kode saham IDX, mis. TLKM"}},
                "required": ["ticker"]}}},
        {"type": "function", "function": {
            "name": "search_news",
            "description": ("CARI berita di arsip OSINT (ribuan headline, full-text) dengan kata "
                            "kunci bebas — mis. 'larangan ekspor nikel', 'BI rate', nama emiten, "
                            "sektor/komoditas. Panggil bila butuh KONTEKS/katalis di luar brief "
                            "yang sudah diberikan (peristiwa terkait, tema sektor)."),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "kata kunci pencarian, mis. 'harga CPO'"}},
                "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "screen_market",
            "description": (
                "SAPU seluruh saham IDX likuid dengan syarat teknikal yang KAMU rumuskan sendiri, "
                "lalu lihat siapa yang memenuhinya hari ini. Pakai ini untuk MENCARI POLA — "
                "menguji dugaan ('apakah oversold + volume ramai memang sedang menyebar?'), "
                "melihat apakah gerakan saham ini bagian dari tema yang lebih luas, atau "
                "menemukan setup yang belum disodorkan siapa pun. "
                "Format filter: '<fitur><operator><angka>' digabung 'and', maks 5 syarat. "
                "Contoh: 'rsi14<30 and vol_vs_avg>2', 'momentum_10d>5 and above_sma20>0', "
                "'consec_down>2 and near_support>0'. "
                "Fitur: rsi14, macd_hist, macd_pos, above_sma20 (1/0), vol_vs_avg, momentum_10d, "
                "cum_change_3d, change_pct, consec_up, consec_down, volatility_20d, "
                "bollinger_pct_b, dist_to_support, dist_to_resistance, near_support (1/0), "
                "near_resistance (1/0), pct_from_52w_high, pct_from_52w_low, bull_divergence (1/0), "
                "bear_divergence (1/0), days_listed, ara_days_5, arb_days_5, price."),
            "parameters": {"type": "object", "properties": {
                "filter": {"type": "string", "description": "mis. 'rsi14<30 and vol_vs_avg>2'"}},
                "required": ["filter"]}}},
        {"type": "function", "function": {
            "name": "track_record",
            "description": (
                "Rekam jejak NYATA sebuah faktor: berapa kali ia muncul di prediksi yang sudah "
                "selesai dan berapa persen menang. Panggil SEBELUM menyandarkan tesis pada satu "
                "faktor — supaya bobotmu berasal dari catatan sistem ini, bukan dari perasaan. "
                "SATU panggilan tanpa argumen sudah mengembalikan SELURUH papan skor — jangan "
                "panggil berulang kali untuk faktor berbeda."),
            "parameters": {"type": "object", "properties": {
                "factor": {"type": "string",
                           "description": "nama faktor, mis. 'asing_beli'. Kosong = semua."}},
                "required": []}}},
        {"type": "function", "function": {
            "name": "remember",
            "description": ("Catat insight TAHAN-LAMA tentang saham ini untuk dirimu sendiri di "
                            "analisis berikutnya (mis. 'volume sering palsu jelang ARA', "
                            "'sangat sensitif harga CPO'). BUKAN kondisi harian. Maks 300 char."),
            "parameters": {"type": "object", "properties": {
                "note": {"type": "string", "description": "insight singkat & spesifik"}},
                "required": ["note"]}}},
        {"type": "function", "function": {
            "name": "suggest_ticker",
            "description": ("Sarankan SATU saham IDX lain yang layak dianalisis siklus ini karena "
                            "temuanmu (peer lebih menarik / rotasi sektor terlihat). Antrean "
                            "dibatasi ketat — sarankan hanya bila sinyalnya jelas."),
            "parameters": {"type": "object", "properties": {
                "ticker": {"type": "string", "description": "kode saham IDX"},
                "reason": {"type": "string", "description": "alasan singkat"}},
                "required": ["ticker", "reason"]}}},
    ]
    return specs, impls


def _analyze_with_tools(ticker: str, quote: dict, note: str | None, horizon: int | None) -> str:
    """Jalur EKSPERIMEN: prompt dasar ramping + tool on-demand. Lempar kalau semua provider gagal
    (analyze() menangkap → fallback inject-all)."""
    specs, impls = _tool_specs(ticker)
    messages = [
        {"role": "system", "content": prompts.system_prompt()},
        {"role": "user", "content": prompts.analyst_user_prompt(
            ticker, quote, note=note, horizon=horizon, lean=True)},
    ]
    out = llm.chat_chain_tools(config.ANALYST_TOOL_CHAIN, messages, specs, impls,
                               max_rounds=config.TOOL_MAX_ROUNDS,
                               timeout=config.ANALYST_TOOL_TIMEOUT)
    view = out["content"].strip()
    trace = out.get("trace") or []
    # Log menyebut LANGKAH KERJANYA, bukan cuma jumlah panggilan: tool apa, dicari apa.
    jejak = " → ".join(
        f"{t['tool']}({next(iter(t['args'].values()), '')})" if t.get("args") else t["tool"]
        for t in trace) or "tanpa tool"
    repo.log("analyst", "analyze",
             f"tesis {ticker} siap (jalur tool via {out.get('provider')}/{out.get('model')}, "
             f"{out.get('tool_calls', 0)} tool-call: {jejak})",
             ticker=ticker, payload={"view": view[:1500], "trace": trace})
    return view


def analyze(ticker: str, quote: dict, note: str | None = None,
            horizon: int | None = None) -> str:
    """Kembalikan teks analisis. Lempar exception kalau LLM gagal (ditangani caller).
    `note` = konteks khusus (target tercapai dll); `horizon` = mandat jendela hari."""
    if config.ANALYST_TOOLS:
        try:
            return _analyze_with_tools(ticker, quote, note, horizon)
        except Exception as e:  # noqa: BLE001 — jalur tool gagal → fallback inject-all (andal)
            repo.log("analyst", "analyze", f"{ticker}: jalur tool gagal ({e}) → inject-all",
                     level="warn", ticker=ticker)
    messages = [
        {"role": "system", "content": prompts.system_prompt()},
        {"role": "user", "content": prompts.analyst_user_prompt(ticker, quote,
                                                                note=note, horizon=horizon)},
    ]
    out = llm.ask_analyst(messages)
    view = out["content"].strip()
    if out.get("reasoning"):
        repo.log("analyst", "analyze", f"reasoning {ticker} ({len(out['reasoning'])} char)",
                 level="debug", ticker=ticker, payload={"reasoning": out["reasoning"][:2000]})
    repo.log("analyst", "analyze", f"tesis {ticker} siap", ticker=ticker,
             payload={"view": view[:1500]})
    return view


def analyze_cached(ticker: str, quote: dict, note: str | None = None,
                   horizon: int | None = None) -> str:
    """Tesis dengan memoization — SATU sumber untuk jalur graf & manual (dedupe dari
    orchestrator). note/horizon = permintaan segar (target tercapai / mandat UI) → jangan
    reuse. Lempar bila semua provider LLM gagal (caller yang memutuskan fallback)."""
    from app.agents import memo
    from app.data import news
    news.fetch_ticker_news(ticker)          # OSINT: berita segar khusus saham ini
    view = memo.get_thesis(ticker, quote) if not (note or horizon) else None
    if view is not None:
        repo.log("engine", "analyze", f"{ticker}: tesis analis dipakai ulang "
                 f"(memo — input tak berubah, hemat token)", ticker=ticker)
        return view
    view = analyze(ticker, quote, note=note, horizon=horizon)
    if not (note or horizon):
        memo.put_thesis(ticker, quote, view)
    return view


def rebut(ticker: str, prior_view: str, trader_critique: str) -> str:
    """Analyst membantah kritik Trader (putaran debat berikutnya).
    System RAMPING — knowledge penuh sudah dipakai di putaran 1, tak perlu dikirim ulang."""
    messages = [
        {"role": "system", "content": prompts.analyst_rebuttal_system()},
        {"role": "user", "content": prompts.analyst_rebuttal_prompt(
            ticker, prior_view, trader_critique)},
    ]
    view = llm.ask_analyst(messages)["content"].strip()
    repo.log("analyst", "analyze", f"bantahan {ticker} siap", ticker=ticker,
             payload={"view": view[:1500]})
    return view
