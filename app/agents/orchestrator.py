"""Orkestrasi siklus agen: fetch -> analyst -> trader -> prediksi -> paper trade.
Plus loop perbaikan diri: evaluasi prediksi jatuh tempo -> catat pelajaran."""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config
from app import market_calendar, model, repo
from app.agents import analyst, council, trader, heuristic, memo
from app.data import news, stocks
from app.eval import WIN_BAND_PCT
from app.eval import edge_floor as _edge_floor
from app.trading import paper

# --- FOKUS/PANTAUAN: saham prioritas dengan mandat gaya ---
# Sumber: (1) config.FOCUS statis (.env, backward-compat) + (2) DB watchlist DINAMIS (star dari
# UI). Gaya di-KLASIFIKASI OTOMATIS per saham (app.agents.style) — user tak perlu set manual.
STYLE_NOTE = {
    "scalp": ("GAYA SCALPING (otomatis — saham fluktuatif/kurang terduga): jendela 1 hari "
              "bursa. Fokus momentum intraday, volume, dan katalis hari ini; target KECIL & "
              "realistis (tutup fee), keluar cepat. Abaikan tesis mingguan."),
    "swing": ("GAYA SWING (otomatis — default pasar Indonesia yang relatif tak terduga): "
              "jendela 3-5 hari bursa. Fokus tren beberapa hari, level support/resistance, "
              "dan katalis minggu ini."),
    "invest": ("GAYA INVESTASI (otomatis — saham TERBUKTI stabil & berkualitas): jendela ~10 "
               "hari bursa (maks). Bobotkan FUNDAMENTAL (valuasi PER/PBV, ROE, growth, dividen, "
               "konsensus analis) DI ATAS teknikal; abaikan noise harian. Nilai kelayakan hold."),
}
_focus_last: dict[str, float] = {}  # ticker → epoch analisis fokus terakhir (throttle token)

# ticker → epoch FLAT-multi-hari terakhir. FLAT tak menghasilkan BARIS prediksi, jadi tak ada
# yang menahan pemilih kandidat memilih saham yang sama tiap siklus: skor teknikal nyaris tak
# berubah intraday → peringkat teratas itu-itu saja. Terukur 31 Jul 2026: BMRI 23x, BBCA 22x,
# PGAS 18x, MEDC 15x, AKRA 9x = 87 dari 97 analisis FLAT yang dibuang, sementara 343 saham naik
# hari itu dan tak pernah sampai ke LLM (8 prediksi tercatat vs 60-83/hari biasanya).
# Cooldown ini melepas slot LLM yang terbuang ke kandidat peringkat BERIKUTNYA.
_flat_last: dict[str, float] = {}

# Jeda sebelum saham ber-FLAT boleh menyita slot LLM lagi. 90 mnt ~ 6 siklus (AGENT_CYCLE_MIN=15)
# → 5 nama teratas melepas ~30 slot/hari ke nama baru tanpa membutakan engine pada mereka.
# ponytail: satu konstanta global, sejajar ADVERSE_FLOOR_PCT/FLAT_BAND_PCT. Naikkan bila engine
# masih berputar di nama yang sama; turunkan bila nama bagus terlewat terlalu lama.
FLAT_RECHECK_MIN = 90


def _auto_style(ticker: str) -> str:
    """Klasifikasi gaya OTOMATIS dari data saham (0 token). Fallback 'swing' bila data kurang."""
    try:
        from app.agents import style as _style
        quote = repo.get_quote(ticker)
        feats = json.loads(quote.get("features_json") or "{}") if quote else {}
        fund = repo.get_fundamentals_row(ticker) or {}
        track = repo.ticker_track_record(ticker)
        return _style.classify_style(feats, fund, track)
    except Exception:  # noqa: BLE001
        return "swing"


def _focus_pass() -> list[str]:
    """Analisis saham PRIORITAS (config.FOCUS + star DB) dgn gaya OTOMATIS — selalu di depan
    hasil scan, ber-throttle per gaya supaya tidak membakar token tiap siklus."""
    done: list[str] = []
    # gabung: star DB (dinamis) + FOCUS env (statis). Star DB pakai gaya auto; FOCUS env
    # boleh punya gaya manual (backward-compat) tapi default juga auto bila 'swing' generik.
    tickers: dict[str, str] = {}
    for t in repo.watched_tickers():
        tickers[t] = _auto_style(t)
    for t, manual in config.FOCUS.items():
        tickers.setdefault(t, manual)   # env tak menimpa star; star menang bila dua-duanya ada
    for ticker, style in tickers.items():
        if style not in config.FOCUS_STYLES:
            style = "swing"
        if time.time() - _focus_last.get(ticker, 0) < config.FOCUS_REFRESH_MIN[style] * 60:
            continue
        try:
            d = _decide_for(ticker, horizon=config.FOCUS_STYLES[style],
                            note=STYLE_NOTE[style])
            if d:
                d["style"] = style
                _commit(d)
                _focus_last[ticker] = time.time()
                done.append(ticker)
                repo.log("engine", "analyze", f"{ticker}: analisis PRIORITAS gaya {style} "
                         f"(auto, horizon {config.FOCUS_STYLES[style]}h)", ticker=ticker)
        except Exception as e:  # noqa: BLE001
            repo.log("engine", "analyze", f"{ticker}: analisis prioritas gagal: {e}",
                     level="warn", ticker=ticker)
    return done


# State throttle siklus-saat-tutup (pra-buka 1×/hari, darurat ber-cooldown).
_CLOSED_STATE = config.DATA_DIR / "closed_cycle.json"
EMERGENCY_COOLDOWN_H = 6     # darurat: maksimal 1 siklus / 6 jam (cegah spam saat tutup)
SHOCK_WINDOW_H = 8           # berita "darurat" = ber-impact ±3 dalam 8 jam terakhir


def _load_closed_state() -> dict:
    try:
        return json.loads(_CLOSED_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_closed_state(st: dict) -> None:
    try:
        _CLOSED_STATE.write_text(json.dumps(st), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _fresh_shock() -> list[dict]:
    """Berita penggerak BESAR & segar (impact ±3 = krisis/perang/default, BUKAN clickbait)."""
    rows = repo.recent_news(limit=50, max_age_hours=SHOCK_WINDOW_H)
    return [r for r in rows if abs(int(r.get("impact") or 0)) >= 3]


def _closed_mode(phase: str) -> str | None:
    """Saat pasar TIDAK buka: boleh jalan sekali untuk outlook? Kembalikan mode atau None.
    - 'pre-open'  : 1× per hari bursa (outlook sesi hari ini dari berita semalam)
    - 'emergency' : ada shock segar & cooldown lewat (reaksi berita darurat)."""
    now = datetime.now(timezone.utc)
    st = _load_closed_state()
    if phase == "pre-open":
        today = now.astimezone(repo._WIB).date().isoformat()
        if st.get("last_preopen") != today:
            return "pre-open"
        return None
    # phase closed / lunch-break / weekend → hanya kalau ada shock & cooldown lewat
    if not _fresh_shock():
        return None
    last = st.get("last_emergency")
    if last:
        age_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
        if age_h < EMERGENCY_COOLDOWN_H:
            return None
    return "emergency"


def _dsum(d: dict) -> str:
    return (f"{d['direction']} {d['probability']:.0f}% /{d['horizon_days']}h "
            f"[{d.get('term', 'pendek')}] -> {d['action']} (exp {d['expected_pct']}%)")


def _scan_one(q: dict, news_scores: dict[str, int]) -> tuple[str, float, dict] | None:
    """Skor heuristik 1 saham dari quote tersimpan (cepat, TANPA API)."""
    feats = json.loads(q.get("features_json") or "{}")
    if not feats:
        return None
    # news per-ticker (dulu HARDCODE 0 → scan buta berita; katalis spt minyak↑ tak terlihat).
    news = news_scores.get(q["ticker"], 0)
    score, _ = heuristic.score_signals(feats, news=news)  # teknikal + berita
    # model terlatih (kalau LOLOS gerbang OOS; sejak 13 Agt 2026 model.json AUC 0,451 < 0,52
    # → predict_proba mengembalikan None dan baris ini TAK BEREFEK). Angka "AUC ~0.63" yang
    # dulu tertulis di sini sudah tak ada lagi; jangan baca komentar sbg klaim kondisi.
    p = model.predict_proba(feats)
    if p is not None:
        score += (p - 0.5) * 5                          # P(naik) tinggi → ranking naik
    return (q["ticker"], score, feats)


def scan_market() -> list[tuple[str, float, dict]]:
    """Scan saham LIKUID (semua thread) untuk menemukan peluang terbaik.

    Gate likuiditas di level universe (audit 2026-07-20): saham beku/nyaris tak
    diperdagangkan tak boleh jadi kandidat sama sekali — bukan cuma dibatasi keyakinannya
    di hilir (Aturan 71). Sinyalnya tak bisa dieksekusi di harga itu, dan historisnya
    justru sumber "edge" palsu. Analisis per-ticker eksplisit (fokus/UI) tak terpengaruh.
    """
    liquid = repo.liquid_tickers()
    quotes = [q for q in repo.all_quotes() if q["ticker"] in liquid]
    if not quotes:  # gate mustahil kosong di pasar normal → jangan diam-diam berhenti scan
        repo.log("engine", "analyze", "gate likuiditas menolak SEMUA ticker — "
                 "cek data harga/turnover, scan dilewati", level="warn")
        return []
    news_scores = repo.news_sentiment_map(24)  # sekali query → dipakai semua saham
    results: list[tuple[str, float, dict]] = []
    with ThreadPoolExecutor(max_workers=config.SCAN_WORKERS) as ex:
        for r in ex.map(lambda q: _scan_one(q, news_scores), quotes):
            if r:
                results.append(r)
    # peluang terbaik = sinyal terkuat (arah apa pun)
    results.sort(key=lambda x: abs(x[1]), reverse=True)
    return results


def _trader_decide_retry(*args, **kw) -> dict:
    """CTO gagal total = badai rate-limit semua provider serentak (audit 2026-07-07: 24%
    percakapan kehilangan CTO → keputusan heuristik buta tesis analis). Tunggu breaker
    pulih (> CB_COOLDOWN) lalu ulang SEKALI — baru menyerah ke heuristik (caller)."""
    try:
        return trader.decide(*args, **kw)
    except Exception as e:  # noqa: BLE001
        repo.log("engine", "analyze", f"CTO gagal ({e}) — tunggu "
                 f"{config.TRADER_RETRY_WAIT_S}s (breaker pulih) lalu ulang 1x", level="warn")
        time.sleep(config.TRADER_RETRY_WAIT_S)
        return trader.decide(*args, **kw)


def _decide_for(ticker: str, horizon: int | None = None,
                note: str | None = None) -> dict | None:
    """Bagian MAHAL (analyst + trader / heuristik) — aman dijalankan paralel.
    Hanya membaca data, TIDAK menulis prediksi/trade (itu dilakukan sekuensial).
    `horizon` = mandat 1/3/5 hari dari UI; `note` = konteks khusus (mis. target baru tercapai)."""
    quote = repo.get_quote(ticker)
    if not quote:
        stocks.fetch_ticker(ticker)
        quote = repo.get_quote(ticker)
    if not quote:
        repo.log("engine", "analyze", f"{ticker}: tidak ada quote, lewati",
                 level="warn", ticker=ticker)
        return None

    position = repo.get_position(ticker)
    cash = repo.get_cash()

    # Faktor STRUKTURAL fired (arus asing/aksi korporasi) — dilekatkan ke keputusan LLM juga
    # supaya SEMUA prediksi (LLM & heuristik) tercatat & dievaluasi per-faktor di laporan.
    _feats = json.loads(quote.get("features_json") or "{}")
    try:
        from app.agents import factors as _factors
        _sigs = [{"name": s["name"], "dir": s["dir"]}
                 for s in _factors.extra_signals(ticker, _feats, quote.get("price") or 0.0)]
    except Exception:  # noqa: BLE001
        _sigs = []

    if config.USE_LLM:
        # JALUR UTAMA: graf MAS terpusat (LangGraph — analis → sentimen → debat → risk gate).
        # Gagal apa pun → jalur manual di bawah (analisisnya termemo, tak dobel token) →
        # heuristik. Tiga lapis; sistem simulasi uang tak pernah buta.
        if config.USE_LANGGRAPH:
            try:
                from app.agents import graph as _graph
                decision = _graph.run_flow(ticker, quote, position, cash,
                                           horizon=horizon, note=note)
                decision["signals"] = _sigs
                repo.log("engine", "analyze", f"{ticker}: graf MAS → "
                         f"{'SEPAKAT' if decision.get('agree') else 'belum sepakat (batas)'}: "
                         f"{_dsum(decision)}", ticker=ticker)
                return decision
            except Exception as e:  # noqa: BLE001
                repo.log("engine", "analyze", f"{ticker}: graf gagal ({e}) → jalur manual",
                         level="warn", ticker=ticker)
        view = None
        try:
            # Memoization di analyst.analyze_cached (satu sumber dgn node graf): berita baru /
            # harga gerak / rezim flip / arus asing / hari baru / TTL → analisis fresh;
            # note/horizon khusus = permintaan segar → jangan reuse.
            view = analyst.analyze_cached(ticker, quote, note=note, horizon=horizon)
        except Exception as e:  # noqa: BLE001
            repo.log("engine", "analyze", f"{ticker}: analis gagal ({e})",
                     level="warn", ticker=ticker)
        if view:
            try:
                # KOORDINASI DINAMIS: trader memutuskan DULU tanpa dewan; dewan hanya
                # dikonsultasikan saat trader RAGU (belum sepakat / keyakinan zona abu-abu /
                # mau BUY) → konsultasi digerakkan keputusan, bukan skrip tetap. Trader yakin
                # → hemat 2 panggilan dewan + 1 panggilan ulang trader.
                # MODE AGEN (TRADER_TOOLS): trigger skrip ini DIMATIKAN — CTO sendiri yang
                # memutuskan konsultasi via tool consult_council (jangan dobel panggil dewan).
                notes = ""
                decision = _trader_decide_retry(ticker, quote, view, position, cash,
                                                council_notes="", horizon=horizon)
                ragu = (not decision.get("agree", True)
                        or 55 <= (decision.get("probability") or 50) <= 68
                        or decision.get("action") == "BUY")
                # Mode pipeline: trigger skrip. Mode agen: CTO harusnya konsultasi SENDIRI via
                # tool; kalau ragu TAPI ia melanggar norma (0 konsultasi) → BACKSTOP skrip tetap
                # memanggil dewan (defense in depth, sefilosofi gate rezim: agen dulu, pagar kemudian).
                cto_consulted = (decision.get("_agent_tools") or {}).get("council_calls", 0) > 0
                if ragu and not cto_consulted:
                    notes = council.discuss(ticker, view)
                    if notes:
                        repo.log("engine", "analyze", f"{ticker}: trader ragu "
                                 f"({decision['direction']} {decision['probability']:.0f}%)"
                                 f"{' — norma dewan dilewati CTO, backstop jalan' if config.TRADER_TOOLS else ''}"
                                 f" → dewan dikonsultasikan", ticker=ticker)
                        decision = trader.decide(ticker, quote, view, position, cash,
                                                 council_notes=notes, horizon=horizon)
                # DEBAT Analyst <-> Trader(CTO) sampai sepakat / batas putaran.
                if config.TRADER_TOOLS and not notes:
                    at = decision.get("_agent_tools") or {}
                    dewan_line = (f"[DEWAN] (mode agen — CTO konsultasi dewan "
                                  f"{at.get('council_calls', 0)}x via tool)")
                elif config.TRADER_TOOLS:
                    dewan_line = f"[DEWAN] (backstop — CTO lewati norma)\n{notes}"
                else:
                    dewan_line = (f"[DEWAN]\n{notes}" if notes
                                  else "[DEWAN] (dilewati — trader yakin)")
                transcript = [dewan_line,
                              f"[ANALYST #1]\n{view}",
                              f"[TRADER/CTO #1] {_dsum(decision)}\n{decision['critique']}"]
                rnd = 1
                # Kebijakan lanjut-debat = trader.should_debate (sumber tunggal dgn router graf):
                # rebuttal hanya bila ada yang dipertaruhkan (hemat ~3.8k token low-stakes).
                while trader.should_debate(decision, rnd):
                    rnd += 1
                    view = analyst.rebut(ticker, view, decision["critique"])
                    decision = trader.decide(ticker, quote, view, position, cash,
                                             council_notes=notes, horizon=horizon)
                    transcript += [f"[ANALYST #{rnd}]\n{view}",
                                   f"[TRADER/CTO #{rnd}] {_dsum(decision)}\n{decision['critique']}"]
                decision["_analyst_view"] = "\n\n".join(transcript)
                decision["signals"] = _sigs  # faktor struktural fired → simpan utk evaluasi
                repo.log("engine", "analyze", f"{ticker}: debat {rnd} putaran → "
                         f"{'SEPAKAT' if decision.get('agree') else 'belum sepakat (batas)'}: "
                         f"{_dsum(decision)}", ticker=ticker)
                return decision
            except Exception as e:  # noqa: BLE001 — trader gagal TAPI analisis analis ada
                if config.LLM_STRICT:
                    # TANPA FALLBACK DIAM-DIAM: tesis analis ada tapi tak ada keputusan CTO.
                    # Mengisi lubang itu dgn skor teknikal lalu menyimpannya sbg prediksi
                    # membuat papan skor mengklaim kerja agen yang tak pernah terjadi.
                    repo.log("engine", "analyze",
                             f"{ticker}: CTO gagal ({e}) — DILEWATI, tak ada prediksi "
                             f"(LLM_STRICT); tesis analis tetap di log analis",
                             level="error", ticker=ticker)
                    return None
                repo.log("engine", "analyze", f"{ticker}: trader gagal ({e}); keputusan "
                         f"heuristik, tesis analis tetap disimpan", level="warn", ticker=ticker)
                d = heuristic.decide(ticker, quote, position, cash, force_horizon=horizon)
                d["signals"] = _sigs
                d["_analyst_view"] = (f"[ANALYST]\n{view}\n\n"
                                      f"[TRADER gagal: {e} → keputusan dari aturan teknikal]")
                return d
        # analis juga gagal → heuristik murni
        if config.LLM_STRICT:
            repo.log("engine", "analyze",
                     f"{ticker}: analis DAN CTO gagal — DILEWATI, tak ada prediksi "
                     f"(LLM_STRICT). Perbaiki provider, jangan biarkan skorer teknikal "
                     f"mengaku sbg keputusan agen", level="error", ticker=ticker)
            return None
        d = heuristic.decide(ticker, quote, position, cash, force_horizon=horizon)
        d["signals"] = _sigs
        d["_analyst_view"] = None
        return d
    d = heuristic.decide(ticker, quote, position, cash, force_horizon=horizon)
    d["signals"] = _sigs
    d["_analyst_view"] = None
    return d


def _apply_regime_gate(d: dict) -> dict:
    """Gate rezim UNIVERSAL (Aturan 79) untuk keputusan LLM MAUPUN heuristik: saat risk-on kuat
    (lean>=1.5), prediksi DOWN berkeyakinan lemah (<66) = indikator bearish lag di pembalikan
    → FLAT. Heuristik sudah menerapkannya sendiri; ini menutup jalur LLM (trader pakai prompt
    RINGKAS tanpa Aturan 79). Kasus nyata 2026-07-03: KETR DOWN 63% dari CTO github saat lean
    +2.96 & pasar +1.4% — mestinya FLAT."""
    if d.get("direction") != "DOWN" or (d.get("probability") or 0) >= 66:
        return d
    reason = ""
    try:
        from app.data import premarket
        lean = premarket.global_brief().get("score", 0.0)
        if lean >= 1.5:
            reason = f"RISK-ON global (lean {lean:+.1f})"
    except Exception:  # noqa: BLE001
        pass
    if not reason:
        # Rezim LOKAL menang atas global utk taruhan DOWN saham IDX: 8-10 Jul 2026 lean
        # global -7.2 (risk-off) tapi IHSG rally 2 hari + breadth 66% + net-buy → DOWN
        # pasca-gate runtuh ke 27.8% (n=18). Gate lama (kunci lean global) buta decoupling.
        try:
            from app.data import flow as _flow
            on, note = _flow.local_reversal()
            if on:
                reason = f"pembalikan LOKAL ({note})"
        except Exception:  # noqa: BLE001
            pass
    if reason:
        # SELL DIPERTAHANKAN (forensik 2026-07-17): gate ini higienis PREDIKSI (jangan catat
        # taruhan DOWN lemah lawan rezim), tapi dulu ikut membalik action SELL→HOLD → keputusan
        # EXIT posisi rugi ikut terblokir (kasus JELI -6.5% ditahan paksa). Keluar dari posisi
        # bukan "melawan tren" — cuma BUY baru/taruhan arah yang perlu digate.
        d["direction"], d["expected_pct"] = "FLAT", 0.0
        if d.get("action") != "SELL":
            d["action"] = "HOLD"
        d["probability"] = min(d.get("probability") or 53.0, 53.0)
        d["target_price"] = d.get("entry_price")
        d.setdefault("key_factors", []).append(
            f"[gate rezim] {reason} + DOWN lemah → FLAT (Aturan 79, indikator bearish lag)")
        repo.log("engine", "analyze", f"{d.get('ticker')}: gate rezim — DOWN lemah saat "
                 f"{reason} → FLAT", ticker=d.get("ticker"))
    return d


def _trend_agreement_gate(d: dict) -> dict:
    """Gate DON'T-FIGHT-THE-TREND, data-backed (audit 2026-07-13, n=1768 resolved berarah):
    prediksi yang MELAWAN tren IHSG rugi SISTEMATIS. Win-rate per (arah × tren IHSG SMA5):
        DOWN @ downtrend = 62.7% (n1192)   ← EDGE utama engine
        UP   @ uptrend   = 36.6% (n41)
        DOWN @ uptrend   = 39.5% (n124)    ← lawan tren, rugi
        UP   @ downtrend = 21.4% (n411)    ← lawan tren, rugi PARAH (biang kerugian)
    Simulasi gate ini: win-rate arah 41.7% → 61.8% (strict). Keyakinan TERBUKTI terbalik utk
    lawan-tren (64%+ UP = 19% menang) → JANGAN pakai keyakinan sbg escape; escape HANYA katalis
    BERITA spesifik emiten (|impact|>=2, 48j) — stok boleh lawan tren pasar bila punya alasan
    sendiri yang nyata. Selain itu lawan-tren → FLAT (bukan taruhan)."""
    if d.get("direction") not in ("UP", "DOWN"):
        return d
    try:
        from app.data import flow as _flow
        uptrend, last, sma5 = _flow.ihsg_uptrend()
    except Exception:  # noqa: BLE001
        return d
    if last <= 0:                       # data IHSG kurang → jangan gate (aman)
        return d
    fights = (d["direction"] == "DOWN" and uptrend) or (d["direction"] == "UP" and not uptrend)
    if not fights:
        return d
    # ESCAPE: katalis berita spesifik emiten (bukan keyakinan — keyakinan lawan-tren tak informatif).
    # Alert "ANOMALI PASAR" (buatan sistem sendiri) DIKECUALIKAN: bukan katalis, bukti sirkuler
    # (harga gerak → "berita" → escape lolos). Baris lama impact±2 masih di DB 48 jam.
    try:
        t = d["ticker"]
        spec = [n for n in repo.news_for_ticker(t, limit=6, max_age_hours=48)
                if t in (n.get("tickers") or "") and abs(int(n.get("impact") or 0)) >= 2
                and not (n.get("title") or "").startswith("ANOMALI PASAR")]
    except Exception:  # noqa: BLE001
        spec = []
    if spec:
        d.setdefault("key_factors", []).append(
            "[gate tren] lawan tren IHSG TAPI ada katalis berita spesifik emiten → dipertahankan")
        return d
    orig = d["direction"]
    d["direction"], d["expected_pct"] = "FLAT", 0.0
    if d.get("action") != "SELL":   # exit posisi tak boleh diblokir gate prediksi (2026-07-17)
        d["action"] = "HOLD"
    d["probability"] = min(d.get("probability") or 53.0, 53.0)
    d["target_price"] = d.get("entry_price")
    d.setdefault("key_factors", []).append(
        f"[gate tren] {orig} melawan tren IHSG (last {last:.0f} vs SMA5 {sma5:.0f}) tanpa "
        f"katalis → FLAT (data: {orig}@lawan-tren <40% menang, jangan lawan tren)")
    repo.log("engine", "analyze", f"{d['ticker']}: gate tren — {orig} lawan tren IHSG "
             f"({last:.0f} vs SMA5 {sma5:.0f}) → FLAT", ticker=d["ticker"])
    return d


def _evidence_gate(d: dict) -> dict:
    """UP wajib bukti NON-teknikal (audit DB 2026-07-08: UP 104/447 = 23.3% benar dgn klaim
    rata-rata 69.9% — mayoritas setup teknikal telanjang 'oversold/rebound'). Pilar yang
    diakui: model statistik sepakat (P>=0.55) / faktor struktural fired (arus asing, aksi
    korporasi) / berita spesifik emiten positif yang BELUM priced-in (Aturan 78). Tanpa satu
    pun → FLAT (multi-hari tak dicatat; companion 1-hari display tetap jalan → outcome UP
    berbukti tetap terkumpul, kalibrasi bisa membuka gate lagi saat rezim berubah)."""
    if d.get("direction") != "UP":
        return d
    pillars: list[str] = []
    q = repo.get_quote(d["ticker"]) or {}
    feats = json.loads(q.get("features_json") or "{}")
    try:
        pup = model.predict_proba(feats)
    except Exception:  # noqa: BLE001
        pup = None
    if pup is not None and pup >= 0.55:
        pillars.append(f"model P(naik)={pup:.0%}")
    if d.get("signals"):
        pillars.append("struktural: " + ",".join(s["name"] for s in d["signals"]))
    try:
        cum3 = feats.get("cum_change_3d") or 0
        # Alert "ANOMALI PASAR" bukan pilar bukti — sistem mendeteksi harganya sendiri bergerak
        # lalu memakai deteksi itu sbg "berita positif" = sirkuler (biang JELI 2026-07-17).
        spec = [n for n in repo.news_for_ticker(d["ticker"], limit=8, max_age_hours=48)
                if d["ticker"] in (n.get("tickers") or "") and int(n.get("impact") or 0) >= 2
                and not (n.get("title") or "").startswith("ANOMALI PASAR")]
        if spec and cum3 < 8:   # sudah melesat >=8%/3hr → berita mengekor = priced-in
            pillars.append(f"berita: {spec[0]['title'][:60]}")
    except Exception:  # noqa: BLE001
        pass
    if pillars:
        d.setdefault("key_factors", []).append("[bukti UP] " + " | ".join(pillars))
        return d
    d.setdefault("key_factors", []).append(
        "[gate bukti] UP teknikal-murni tanpa model/struktural/berita → FLAT "
        "(UP telanjang historis 23% benar, n=447)")
    d["direction"], d["expected_pct"], d["action"], d["size_pct"] = "FLAT", 0.0, "HOLD", 0.0
    d["probability"] = min(d.get("probability") or 53.0, 53.0)
    d["target_price"] = d.get("entry_price")
    repo.log("engine", "analyze", f"{d['ticker']}: gate bukti — UP tanpa pilar non-teknikal "
             f"→ FLAT", ticker=d["ticker"])
    return d


def _proven_loss_gate(d: dict) -> dict:
    """Lengan taruhan yang TERBUKTI di bawah base rate pasar tak boleh bertaruh lagi.

    Audit 2026-08-31, dijalankan dengan gerbang bukti repo ini sendiri (unit = TANGGAL,
    bootstrap blok): dari semua populasi yang diuji, SATU-SATUNYA yang lolos adalah temuan
    NEGATIF — DOWN horizon>=2, n=114, menang 34,2% vs base rate 47,6%, CI selisih
    [-36,3, -3,2] pp atas 23 tanggal, side="below" PASS. Sisanya (taruhan nyata gabungan,
    UP h>=2, companion 1-hari) semuanya "belum terbukti". Jadi satu-satunya hal yang benar-
    benar diketahui tentang keterampilan arah mesin ini adalah bahwa satu lengannya rugi.

    Gerbang ini generik dan MEMBUKA SENDIRI: ia menanyakan vonis tiap kali, jadi begitu
    lengannya tak lagi lolos side="below" ia kembali bertaruh. Datanya tetap mengalir lewat
    companion 1-hari (horizon==1, tak ditradingkan) sehingga lengan yang ditutup tetap bisa
    membuktikan diri. SELL tak diblokir — keluar dari posisi bukan taruhan arah baru
    (pola sama dengan _apply_regime_gate/_trend_agreement_gate).
    """
    if d.get("direction") not in ("UP", "DOWN") or (d.get("horizon_days") or 3) < 2:
        return d
    try:
        from app.agents import skill
        v = skill.arm_verdict(d["direction"], min_horizon=2)
    except Exception:  # noqa: BLE001
        return d
    if not v.get("pass"):
        return d
    orig = d["direction"]
    d["direction"], d["expected_pct"] = "FLAT", 0.0
    if d.get("action") != "SELL":
        d["action"], d["size_pct"] = "HOLD", 0.0
    d["probability"] = min(d.get("probability") or 53.0, 53.0)
    d["target_price"] = d.get("entry_price")
    # Arah ASLI disimpan supaya veto "jangan jual cuma karena sinyal hilang" di
    # paper.pre_trade_checks tahu FLAT ini hasil tulis-ulang gerbang, bukan tesis yang datar.
    # Tanpa ini gerbang lengan-rugi kehilangan jalan keluarnya sendiri saat aktif lagi.
    d["_arm_closed_from"] = orig
    d.setdefault("key_factors", []).append(
        f"[gate lengan rugi] {orig} h>=2 terbukti DI BAWAH base rate pasar → FLAT. {v['detail']}")
    repo.log("engine", "analyze", f"{d['ticker']}: gate lengan rugi — {orig} h>=2 "
             f"({v.get('winrate')}% vs base {v.get('baseline')}%) → FLAT", ticker=d["ticker"])
    return d


def _calibrate(d: dict) -> dict:
    """Kalibrasi DETERMINISTIK untuk keputusan LLM — aturan 'jangan overconfident' di prompt
    terbukti TAK dipatuhi (audit 2026-07-08, n=1.532 resolved berarah: klaim 70-80% realisasi
    47.1%, klaim 60-70% justru 53.3% — klaim tak membawa informasi). Jalur heuristik sudah
    terkalibrasi di heuristic.decide (flag _calibrated) → jangan di-shrink dua kali."""
    if d.get("_calibrated") or d.get("direction") not in ("UP", "DOWN"):
        return d
    try:
        from app.agents import skill
        # keep_spread=0: sebaran klaim CTO tak dipertahankan karena terukur TAK membawa
        # informasi (lihat docstring di atas + skill.calibrated_probability). Yang dijaga
        # sebarannya cuma jalur heuristik, yang angkanya turunan deterministik dari skor.
        cal = skill.calibrated_probability(d["direction"], d["probability"], keep_spread=0.0)
    except Exception:  # noqa: BLE001
        return d
    if abs(cal - (d.get("probability") or 0)) >= 5:
        d.setdefault("key_factors", []).append(
            f"[kalibrasi] klaim CTO {d['probability']:.0f}% → {cal:.0f}% "
            f"(realisasi historis arah {d['direction']})")
    d["probability"] = cal
    return d


def _narrate(d: dict) -> dict:
    """Alasan KAYA & deterministik (0 token LLM): user melihat KENAPA per pilar — teknikal,
    berita spesifik emiten (judul+sentimen), arus asing resmi, rezim makro — plus rekam jejak
    sinyal serupa; bukan sekadar 'skor komposit 2.1' / 'SMA5 & SMA20'."""
    t = d["ticker"]
    head = (f"{t} {d['direction']} {d.get('expected_pct') or 0:+.1f}%/"
            f"{d['horizon_days']}h — keyakinan {d['probability']:.0f}%")
    try:
        res = [r for r in repo.resolved_predictions(300) if r["direction"] == d["direction"]]
        if len(res) >= 20:
            w = sum(1 for r in res if r["outcome"] == "win")
            head += f" (sinyal {d['direction']} historis {w}/{len(res)} benar)"
    except Exception:  # noqa: BLE001
        pass
    lines = [head]
    fk = [str(f) for f in (d.get("key_factors") or []) if not str(f).startswith("[")][:3]
    if fk:
        lines.append("Teknikal: " + "; ".join(fk))
    try:
        spec = [n for n in repo.news_for_ticker(t, limit=6, max_age_hours=48)
                if t in (n.get("tickers") or "")]
        if spec:
            lines.append("Berita: " + " | ".join(
                f"[{n['sentiment']}{int(n['impact'] or 0):+d}] {(n['title'] or '')[:70]}"
                for n in spec[:2]))
        else:
            lines.append("Berita: tidak ada berita spesifik emiten 48 jam terakhir — "
                         "sinyal murni teknikal/makro (bobot lebih rendah)")
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.data import idxflow
        lines.append("Asing: " + idxflow.foreign_brief(t))
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.data import premarket
        g = premarket.global_brief()
        lines.append(f"Rezim global: {g.get('lean', '?')} (lean {g.get('score', 0):+.1f})")
    except Exception:  # noqa: BLE001
        pass
    base = (d.get("reasoning") or "").strip()
    if base and not base.startswith("skor komposit"):   # narasi menggantikan baris skor kering
        lines.append(f"CTO: {base[:400]}")
    d["reasoning"] = "\n".join(lines)
    return d


def _unjudged(made_iso: str) -> bool:
    """True kalau prediksi BELUM layak dinilai: belum ada satu pun sesi bursa yang TUTUP
    sejak ia dibuat, jadi harga acuan masih angka yang sama dengan entry (0.0% palsu).
    Menggantikan penjaga 'hari WIB sama' yang bocor di Sabtu/Minggu/libur dan pada prediksi
    yang dibuat setelah closing — lihat market_calendar.has_new_session."""
    try:
        made = datetime.fromisoformat(made_iso)
        if made.tzinfo is None:
            made = made.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return False
    return not market_calendar.has_new_session(made, datetime.now(timezone.utc))


def _supersede_one(old: dict, price: float) -> None:
    """Tutup prediksi lama yang DIGANTIKAN analisis lebih baru. Dinilai menang/kalah HANYA
    kalau taruhannya sudah benar-benar jalan sampai horizon (`repo.prediction_is_due`);
    kalau belum → 'superseded' (netral: tak dihitung win/loss, tak muncul di UI).

    Sebelum 2026-08-13 syaratnya cuma "sudah ada 1 sesi bursa yang tutup" — untuk taruhan
    3-5 hari itu berarti divonis pada penutupan HARI PERTAMA, dengan ambang ±0,5% yang
    dirancang untuk seluruh horizon. Audit n=90 prediksi headline: 34 divonis pada umur
    rata-rata 0,61 hari (horizon rata-rata 3,9 hari) dan menang 17,6%, vs 39,3% untuk yang
    jalan penuh — angka 'akurasi' yang turun itu sebagian besar salah-ukur, bukan salah-tebak.
    Bracket target/adverse (`_resolve_target_hits`) TIDAK terpengaruh: itu penutupan dini yang
    sah karena harga benar-benar menembus jarak target (simetris dua sisi)."""
    if repo.prediction_is_due(old, datetime.now(timezone.utc)):
        _finalize(old, price)
    else:
        repo.supersede_prediction(old["id"])


def _supersede_stale(ticker: str, keep_direction: str | None, keep_horizon: int | None) -> None:
    """Prediksi MAIN (horizon!=1) LAMA milik ticker ini yang TAK cocok dgn keputusan baru
    sudah usang (analisis baru = situasi baru) → tutup SEKARANG, jangan dibiarkan menumpuk.
    Same-day → superseded (netral); hari lalu → dinilai jujur (lihat `_supersede_one`).

    Root cause bug "duplicate prediction" (2026-07-13): dedup lama (`open_prediction_id`)
    hanya cek (ticker,arah,horizon) PERSIS SAMA — kalau LLM sedikit ubah horizon (3→5→7h)
    atau arah antar siklus, prediksi lama tak pernah match & tak pernah ditutup → menumpuk
    (ditemukan ANTM 8 baris open FLAT/UP/DOWN x horizon beda BERSAMAAN). Companion 1-hari
    (horizon==1) dilewati di sini — siklus hidupnya sendiri, diurus `_commit_1d`.
    keep_direction/keep_horizon=None → tutup SEMUA main (dipakai saat keputusan baru FLAT
    multi-hari = tak ada taruhan baru yang dipertahankan)."""
    quote = repo.get_quote(ticker)
    if not quote:
        return
    for old in repo.open_predictions_for_ticker(ticker):
        if old["horizon_days"] == 1:
            continue
        if old["direction"] == keep_direction and old["horizon_days"] == keep_horizon:
            continue  # persis sama keputusan baru → jalur dedup existing di bawah yg urus
        _supersede_one(old, quote["price"])


def _commit(decision: dict) -> int:
    """Simpan prediksi + eksekusi paper trade. Dipanggil SEKUENSIAL (hindari race kas).
    ANTI-DUPLIKAT: sinyal sama (ticker+arah) yang masih open TIDAK dicatat ulang — 1 sinyal
    = 1 taruhan = 1 baris statistik. Trade tetap dieksekusi (posisi bisa berubah)."""
    # Setiap prediksi dapat GAYA (scalp/swing/invest) — dari mandat fokus bila ada, else auto
    # klasifikasi dari data ("analisis penuh hari ini": ini scalping atau layak invest?).
    decision.setdefault("style", _auto_style(decision["ticker"]))
    decision = _apply_regime_gate(decision)   # universal: tutup jalur LLM (bukan cuma heuristik)
    decision = _trend_agreement_gate(decision)  # DON'T FIGHT TREND: lawan-tren → FLAT (41.7→61.8%)
    decision = _evidence_gate(decision)       # UP tanpa pilar non-teknikal → FLAT (23% benar)
    decision = _calibrate(decision)           # klaim LLM → realisasi historis (deterministik)
    decision = _proven_loss_gate(decision)    # lengan terbukti di bawah base rate → FLAT
    # urutan: SESUDAH _calibrate supaya vonis lengan dinilai atas keyakinan yang sama dengan
    # yang dicatat, dan SEBELUM re-gate BUY di bawah supaya aksi ikut dibatalkan.
    # RE-GATE BUY SETELAH kalibrasi: action ditentukan CTO saat klaim masih tinggi — kalibrasi
    # menurunkan keyakinan tapi dulu action tetap jalan (kasus ESSA 2026-07-10: BUY tereksekusi
    # dgn keyakinan terkalibrasi 52% < ambang 60). Keyakinan final < 60 → tak ada dasar beli.
    _buy_floor = _edge_floor("UP", decision.get("horizon_days") or 3, config.MIN_EDGE_PP)
    _why = ""
    if decision.get("action") == "BUY":
        if (decision.get("probability") or 0) < _buy_floor:
            _why = (f"keyakinan terkalibrasi {decision.get('probability', 0):.1f}% < lantai "
                    f"{_buy_floor:.1f}% (base rate UP + {config.MIN_EDGE_PP:.0f} pp)")
        else:
            # Gerbang uang yang sama dgn heuristic.decide — ditegakkan lagi di sini karena
            # jalur LLM tak melewatinya (defense in depth, pola sama dgn gate rezim).
            try:
                from app.agents import skill as _sk
                _v = _sk.arm_verdict("UP", 2, side="above")
                if not _v.get("pass"):
                    _why = f"lengan UP belum terbukti di atas base rate — {_v.get('detail', '')}"
            except Exception:  # noqa: BLE001
                pass
    if _why:
        decision["action"], decision["size_pct"] = "HOLD", 0.0
        decision.setdefault("key_factors", []).append(f"[gerbang uang] BUY dibatalkan — {_why}")
        repo.log("engine", "analyze", f"{decision['ticker']}: BUY dibatalkan — {_why}",
                 ticker=decision["ticker"])
    decision = _narrate(decision)             # alasan kaya per-pilar, 0 token
    # FLAT multi-hari = "tak ada sinyal", BUKAN taruhan. Menang hanya bila |gerak|<1% —
    # realisasi cuma 16-24% (forensik 2026-07-07, n=153 non-display) → mencatatnya menyeret
    # win-rate & kalibrasi. Trade (mis. SELL exit posisi) tetap dieksekusi; companion 1-hari
    # tetap dicatat (display "besok", band ±1% masuk akal utk 1 hari, realisasi 45%).
    # FLAT tidak dicatat sebagai taruhan pada horizon mana pun. Diuji terhadap dasar pasar
    # yang dicocokkan per tanggal (tools/gate_trial.py): win-rate FLAT ada di bawah dasarnya
    # sendiri, artinya menebak "diam" pada saham likuid acak lebih sering benar. Trade tetap
    # dieksekusi di bawah; yang dihentikan hanya pencatatannya.
    flat_skip = decision["direction"] == "FLAT"
    _supersede_stale(decision["ticker"],
                     None if flat_skip else decision["direction"],
                     None if flat_skip else decision.get("horizon_days"))
    if flat_skip:
        paper.act_on_decision(decision, None)
        if decision.get("_analyst_view"):      # transkrip debat tetap terarsip (UI/tesis)
            repo.save_conversation(decision["ticker"], decision["_analyst_view"],
                                   f"KRITIK ATAS ANALIS: {decision.get('critique', '-')}",
                                   f"FLAT {decision['probability']:.0f}% -> HOLD (tak dicatat "
                                   f"sbg taruhan)", None)
        repo.log("engine", "analyze", f"{decision['ticker']}: FLAT multi-hari — tak dicatat "
                 f"sbg prediksi (bukan sinyal)", ticker=decision["ticker"])
        _flat_last[decision["ticker"]] = time.time()   # lepas slot LLM ke kandidat berikutnya
        _commit_1d(decision["ticker"])
        return 0
    existing = repo.open_prediction_id(decision["ticker"], decision["direction"],
                                       decision["horizon_days"])
    if existing:
        repo.log("engine", "analyze", f"{decision['ticker']}: prediksi {decision['direction']} "
                 f"masih open (id {existing}) — duplikat di-skip, trade tetap dievaluasi",
                 ticker=decision["ticker"])
        paper.act_on_decision(decision, existing)
        if decision.get("horizon_days") != 1:   # companion 1-hari tetap dicoba (dedup sendiri)
            _commit_1d(decision["ticker"])
        return existing
    pid = repo.save_prediction({
        "ticker": decision["ticker"],
        "direction": decision["direction"],
        "probability": decision["probability"],
        "horizon_days": decision["horizon_days"],
        "entry_price": decision["entry_price"],
        "target_price": decision["target_price"],
        "expected_pct": decision["expected_pct"],
        "reasoning": decision["reasoning"],
        "factors": {"key_factors": decision["key_factors"],
                    "critique": decision["critique"],
                    "signals": decision.get("signals") or [],  # faktor struktural fired → evaluasi
                    "risk": decision.get("risk"),              # verdict Risk Agent (frekuensi veto)
                    "term": decision.get("term"),              # pendek|panjang|keduanya (badge UI)
                    "style": decision.get("style"),            # scalp|swing|invest (jalur fokus)
                    "raw_prob": decision.get("raw_prob"),      # bayangan: keyakinan pra-kalibrasi
                    "score": decision.get("score"),            # bayangan: skor komposit heuristik
                    "cto_model": decision.get("_cto_model")},  # model yang benar-benar memutus
    })
    paper.act_on_decision(decision, pid)

    # Percakapan agen HANYA untuk prediksi ber-analis (debat LLM) — heuristik murni
    # tak punya analisis untuk dibaca, jadi tak mengotori panel laporan.
    analyst_text = decision.get("_analyst_view")
    if analyst_text:
        factors = "; ".join(str(f) for f in (decision.get("key_factors") or []))
        trader_text = (f"KRITIK ATAS ANALIS: {decision.get('critique', '-')}\n"
                       f"FAKTOR KUNCI: {factors or '-'}\n"
                       f"ALASAN KEPUTUSAN: {decision.get('reasoning', '-')}")
        summary = (f"{decision['direction']} {decision['probability']:.0f}% "
                   f"/{decision['horizon_days']}h -> {decision['action']} "
                   f"(target {decision['target_price']}, exp {decision['expected_pct']}%)")
        repo.save_conversation(decision["ticker"], analyst_text, trader_text, summary, pid)

    # COMPANION 1-HARI (besok): forecast arah pendek utk saham ini — TIDAK memicu trade
    # (murni tampilan "naik/turun besok"). Multi-hari (utama) tetap yang men-drive paper trade.
    if decision.get("horizon_days") != 1:
        _commit_1d(decision["ticker"])
    return pid


def _commit_1d(ticker: str) -> None:
    """Catat prediksi 1-hari (besok) dari heuristik — forecast display, tanpa paper trade."""
    quote = repo.get_quote(ticker)
    if not quote:
        return
    d = heuristic.decide(ticker, quote, repo.get_position(ticker), repo.get_cash(), force_horizon=1)
    # Tutup companion 1-hari lama yang ARAHNYA BEDA (usang) — tanpa ini FLAT/1 & UP/1 bisa
    # nyala bersamaan utk ticker yang sama saat heuristik flip antar-siklus (bug sama dgn
    # _supersede_stale, versi companion).
    for old in repo.open_predictions_for_ticker(ticker):
        if old["horizon_days"] == 1 and old["direction"] != d["direction"]:
            _supersede_one(old, quote["price"])  # same-day → superseded, hari lalu → dinilai
    if d["direction"] == "FLAT":
        return  # 551 dari 595 FLAT di buku lahir di sini; lihat catatan flat_skip di _commit
    if repo.open_prediction_id(ticker, d["direction"], 1):
        return  # sudah ada prediksi 1-hari arah sama yang open
    repo.save_prediction({
        "ticker": ticker, "direction": d["direction"], "probability": d["probability"],
        "horizon_days": 1, "entry_price": d["entry_price"], "target_price": d["target_price"],
        "expected_pct": d["expected_pct"], "reasoning": "prediksi 1-hari (besok) — forecast.",
        "style": _auto_style(ticker),
        "factors": {"key_factors": d["key_factors"], "critique": "(1-hari)",
                    "signals": d.get("signals") or [],
                    "raw_prob": d.get("raw_prob"), "score": d.get("score")},
    })


def run_one(ticker: str, horizon: int | None = None, note: str | None = None) -> dict | None:
    """Analisis + keputusan + simpan + trade untuk satu saham (endpoint manual / analisis-ulang).
    `horizon` = mandat 1/3/5 hari (tombol UI); `note` = konteks (mis. target baru tercapai)."""
    d = _decide_for(ticker, horizon=horizon, note=note)
    if d:
        _commit(d)
    return d


# Hanya catat prediksi heuristik yang cukup yakin & berarah (kurangi prediksi sampah).
# Sekarang dinyatakan sbg EDGE (pp di atas base rate arah+horizon), bukan angka mutlak —
# alasan sama dgn config.MIN_EDGE_PP. Nilai lama 58/63 mutlak setara ~+17/+22 pp di atas base
# rate UP h=3 (40,9%), jauh lebih ketat daripada maksudnya semula ("ada sinyal berarah").
HEUR_MIN_EDGE_PP = 8.0
# Sesi akhir (>=14:00 WIB) terbukti buruk: win-rate 40-42% vs 49-56% pagi (n=655, z<-2.5,
# mining 2026-07-10) — fitur harian belum final + kejar penggerak hari itu. Bar dinaikkan.
LATE_HOUR_WIB = 14
LATE_MIN_EDGE_PP = 13.0


def _heuristic_commit(ticker: str) -> bool:
    """Catat prediksi heuristik (tanpa LLM) untuk saham hasil scan. Lewati sinyal lemah/FLAT."""
    q = repo.get_quote(ticker)
    if not q:
        return False
    d = heuristic.decide(ticker, q, repo.get_position(ticker), repo.get_cash())
    if d["direction"] == "FLAT":
        return False
    edge_pp = (LATE_MIN_EDGE_PP if datetime.now(repo._WIB).hour >= LATE_HOUR_WIB
               else HEUR_MIN_EDGE_PP)
    min_prob = _edge_floor(d["direction"], d.get("horizon_days") or 3, edge_pp)
    if d["probability"] < min_prob:
        return False  # sinyal lemah → jangan dicatat (akurasi terukur lebih jujur)
    d["_analyst_view"] = None
    _commit(d)
    return True


def run_cycle() -> dict:
    """Scan SEMUA saham (cari yang terbaik) → LLM hanya untuk kandidat top.

    - Heuristik men-scan seluruh universe (multi-thread, tanpa API) = "jalankan
      semua identifikasi".
    - LLM (deepseek+minimax) hanya menganalisis kandidat TERBAIK → hemat kuota,
      hindari 429, fokus ke peluang riil.
    - Saat pasar BUKA, jumlah kandidat LLM digandakan (lebih agresif cari peluang).
    """
    phase = stocks.market_phase()
    # Saat pasar BUKA: jalan normal. Saat TUTUP: harga beku → JANGAN spam prediksi tiap siklus,
    # KECUALI (a) pra-buka 1×/hari (outlook hari ini) atau (b) ada berita DARURAT semalam
    # (impact ±3). Horizon dihitung dalam hari BURSA (repo) → prediksi tutup tetap valid.
    mode = "open"
    if phase != "open":
        mode = _closed_mode(phase)
        if not mode:
            repo.log("engine", "analyze", f"pasar {phase} — lewati siklus (tak ada katalis)")
            return {"phase": phase, "scanned": 0, "predictions": 0, "skipped": True}
        now = datetime.now(timezone.utc)
        st = _load_closed_state()
        if mode == "pre-open":
            st["last_preopen"] = now.astimezone(repo._WIB).date().isoformat()
        else:
            shocks = _fresh_shock()
            st["last_emergency"] = now.isoformat()
            repo.log("engine", "analyze", f"DARURAT: {len(shocks)} berita impact±3 saat pasar "
                     f"{phase} → outlook sesi berikutnya: {shocks[0].get('title', '')[:80]}")
        _save_closed_state(st)
        repo.log("engine", "analyze", f"pasar {phase} — siklus '{mode}' (outlook, bukan intraday)")

    exited = paper.check_exits()  # jaring pengaman exit dulu (stop-loss/take-profit/kelamaan)
    if exited:
        repo.log("engine", "trade", f"auto-exit: {', '.join(exited)}")

    scored = scan_market()
    if not scored:
        # Dua sebab mungkin: belum ada quote, ATAU gate likuiditas menolak semua (yang
        # sudah mencetak warn spesifik sendiri di scan_market). Jangan menebak di sini.
        repo.log("engine", "analyze", "scan: tak ada kandidat (quote kosong atau gate "
                 "likuiditas menolak semua — lihat warn sebelumnya), lewati", level="warn")
        return {"phase": phase, "scanned": 0, "predictions": 0}

    n_llm = config.CANDIDATES_PER_CYCLE * (2 if phase == "open" else 1)
    # HEMAT #1: kandidat yang arah teknikalnya KONFLIK dgn model dilewati LLM — terukur
    # (2.739 sampel): arah saat konflik cuma 39.8% benar vs 59.8% saat sepakat; heuristik
    # toh akan mem-FLAT-kannya. Deep-dive LLM pada koin-lempar = token terbuang.
    def _aligned(s: float, feats: dict) -> bool:
        if abs(s) < 1.0:
            return True
        p = model.predict_proba(feats)
        return p is None or (p >= 0.5) == (s > 0)

    # Saham yang BARU SAJA di-FLAT-kan dilewati dulu (lihat _flat_last): slotnya turun ke
    # kandidat peringkat berikutnya, bukan hangus. Cek dict didahulukan — murah, dan
    # menghindarkan _aligned memanggil model.predict_proba untuk nama yang toh dilewati.
    _fresh = time.time() - FLAT_RECHECK_MIN * 60
    top_llm = [t for t, s, f in scored
               if _flat_last.get(t, 0) < _fresh and _aligned(s, f)][:n_llm]
    # HEMAT #2: posisi dipegang ditinjau-ulang LLM HANYA saat ada perubahan berarti (memo
    # tesis invalid: harga >1.2% / berita emiten / rezim flip / arus asing / hari baru /
    # TTL 3 jam). Stop-loss/take-profit/kelamaan tetap dijaga paper.check_exits tiap siklus.
    # Sebelumnya tiap posisi memicu analis+CTO tiap 12 menit walau tak ada apa-apa.
    held = []
    for pos in repo.get_positions():
        q = repo.get_quote(pos["ticker"])
        if not q or memo.get_thesis(pos["ticker"], q) is None:
            held.append(pos["ticker"])
    top_llm = list(dict.fromkeys(top_llm + held))
    top_record = [t for t, _, _ in scored[:config.SCAN_RECORD_TOP]]
    repo.log("engine", "analyze",
             f"scan {len(scored)} saham ({phase}). Kandidat terbaik → LLM: "
             f"{', '.join(top_llm)}")

    made = 0
    rows0 = repo.count_predictions()   # pembanding jujur: berapa BARIS yang benar-benar lahir
    # 0) FOKUS: saham prioritas user (mandat gaya scalp/swing/invest) — selalu duluan,
    #    ber-throttle per gaya. Masuk llm_done supaya tak dianalisis dobel di bawah.
    focus_done = _focus_pass()
    made += len(focus_done)
    top_llm = [t for t in top_llm if t not in focus_done]  # jangan analisis dobel

    # 1) LLM deep-dive kandidat terbaik. _decide_for PARALEL (read-only, aman) lewat
    #    LLM_PARALLEL worker — provider beda jalan serempak (throttle per-provider cegah 429).
    #    _commit SEKUENSIAL (hindari race kas; buy() re-cek kas/limit saat commit).
    llm_done: set[str] = set(focus_done)

    def _safe_decide(t: str) -> tuple[str, dict | None]:
        try:
            return t, _decide_for(t)
        except Exception as e:  # noqa: BLE001
            repo.log("engine", "analyze", f"{t}: error analisis: {e}", level="error", ticker=t)
            return t, None

    workers = max(1, min(len(top_llm), config.LLM_PARALLEL))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        decided = list(ex.map(_safe_decide, top_llm))  # urutan top_llm dipertahankan
    # ABLASI: potret keluaran MENTAH tiap keputusan SEBELUM _commit menerapkan gerbang —
    # `_commit` mengubah dict itu di tempat, dan yang dibandingkan antar lengan harus keluaran
    # mentah (lihat app.agents.shadow). Perekamannya sendiri dikerjakan sesudah commit supaya
    # panggilan lengan llm1 tak menunda pencatatan prediksi produksi.
    _bayangan = [(t, {"direction": d["direction"], "probability": d.get("probability"),
                      "horizon_days": d.get("horizon_days"),
                      "_cto_model": d.get("_cto_model"), "raw_prob": d.get("raw_prob")})
                 for t, d in decided if d]
    for t, d in decided:
        if not d:
            continue
        try:
            _commit(d)
            made += 1
            llm_done.add(t)
        except Exception as e:  # noqa: BLE001
            repo.log("engine", "analyze", f"{t}: commit gagal: {e}", level="error", ticker=t)

    try:
        from app.agents import shadow
        shadow.record_many(_bayangan)
    except Exception as e:  # noqa: BLE001 — papan skor tak boleh menjatuhkan siklus
        repo.log("shadow", "record", f"gagal: {e}", level="warn")

    # 2) ATENSI AGEN: saham yang DISARANKAN analis via suggest_ticker saat menganalisis
    #    (peer lebih menarik / rotasi sektor) — agen ikut mengarahkan siklus, bukan hanya
    #    skrip scan. Cap AGENT_SUGGEST_MAX (pagar token); kosong di mode pipeline.
    for t, reason in analyst.drain_suggestions()[:config.AGENT_SUGGEST_MAX]:
        if t in llm_done:
            continue
        repo.log("engine", "analyze", f"{t}: dianalisis atas SARAN agen ({reason})", ticker=t)
        try:
            d = _decide_for(t, note=f"Kamu sendiri yang menyarankan memeriksa saham ini: {reason}")
            if d:
                _commit(d)
                made += 1
                llm_done.add(t)
        except Exception as e:  # noqa: BLE001
            repo.log("engine", "analyze", f"{t}: saran agen gagal dianalisis: {e}",
                     level="warn", ticker=t)

    # 3) Prediksi heuristik untuk sisa top peluang (agar dashboard punya sinyal luas)
    for t in top_record:
        if t in llm_done:
            continue
        try:
            if _heuristic_commit(t):
                made += 1
        except Exception:  # noqa: BLE001
            continue

    paper.snapshot()
    # `made` menghitung keputusan yang diproses, bukan baris yang lahir: `_commit` mengembalikan
    # id yang sama saat sinyal duplikat di-skip dan 0 saat digerbang. Angka yang dibaca orang
    # harus bisa dihitung ulang dari tabel, jadi baris nyata dilaporkan sebagai angka utama.
    written = repo.count_predictions() - rows0
    repo.log("engine", "analyze",
             f"siklus selesai: {len(scored)} discan, {len(llm_done)} via LLM, "
             f"{written} prediksi BARU ({made} keputusan diproses)")
    return {"phase": phase, "mode": mode, "scanned": len(scored),
            "llm": len(llm_done), "predictions": written, "decisions": made}


# Lantai jarak penutupan awal agar tak resolve karena noise intraday kecil. Berlaku DUA sisi.
ADVERSE_FLOOR_PCT = 2.0

# Band "tak bergerak" untuk menilai prediksi FLAT. 1.0% dulu terlalu ketat: median |gerak|
# 1 hari saham yang dipilih engine = 1.75% (n=99, audit 2026-07-27) → gerak normal dilabeli
# meleset, FLAT cuma 32% benar. ponytail: satu konstanta global; naikkan ke band per-saham
# (mis. ATR) kalau presisi per-saham benar-benar dibutuhkan.
FLAT_BAND_PCT = 1.75


def is_win(direction: str, actual_pct: float) -> bool:
    """SATU definisi menang untuk seluruh repo: |gerak| > 0,5% ke arah yang diklaim (FLAT
    menang bila diam di dalam band). Dipakai `_finalize` (jalur produksi) DAN
    `shadow.resolve_due` (papan skor ablasi) — kalau keduanya punya salinan sendiri, dua
    lengan bisa diam-diam dinilai dengan ambang berbeda dan tabel perbandingannya bohong."""
    if direction == "UP":
        return actual_pct > WIN_BAND_PCT
    if direction == "DOWN":
        return actual_pct < -WIN_BAND_PCT
    return abs(actual_pct) <= FLAT_BAND_PCT


def _resolve_target_hits() -> int:
    """Prediksi 'open' yang harga sudah MENEMBUS target (menang) ATAU bergerak LAWAN arah
    SEJAUH jarak-target (kalah) sebelum horizon habis -> tutup SEKARANG (BRACKET SIMETRIS,
    jujur dua sisi), catat pelajaran, lalu analisis ULANG saham itu (situasi baru = tesis baru).

    Simetri PENTING (anti-cheat, audit 2026-07-02): dulu menang tutup di jarak-target (mis. 3%)
    tapi kalah baru tutup di 5% tetap → menang sistematis lebih cepat = win-rate menggelembung
    (early-close 84% vs horizon 48%). Kini AMBANG kalah = jarak target yang sama (min 2%) →
    +X menang ⇔ −X kalah, taruhan yang jujur."""
    n = 0
    for p in repo.open_predictions():
        if not p["target_price"] or not p["entry_price"] or p["direction"] not in ("UP", "DOWN"):
            continue
        # JANGAN nilai menang/kalah di hari yang sama prediksi dibuat — gerakan intraday =
        # noise untuk taruhan multi-hari (permintaan user: status jangan salah sebelum hari
        # berakhir). Bracket target/adverse baru aktif mulai hari bursa BERIKUTNYA.
        if _unjudged(p["ts"]):
            continue
        quote = repo.get_quote(p["ticker"])
        if not quote:
            continue
        price = quote["price"]
        actual_pct = (price / p["entry_price"] - 1) * 100
        # SATU jarak untuk DUA sisi. Simetri lama masih bocor lewat ADVERSE_FLOOR: menang
        # ditutup saat harga menyentuh target (median jarak 1,13%, 77% taruhan <2%) tapi
        # kalah baru pada 2% → ambang menang lebih dekat daripada ambang kalah. Terukur
        # (audit 2026-07-27): bucket target<2% menang 50,9% vs bucket simetris 46,8%; median
        # gerak saat menang -1,48% vs saat kalah +2,26% = sidik jari ambang timpang. Pakai
        # |jarak| juga menutup target yang berlawanan arah (ENRG id339: UP dgn target DI BAWAH
        # entry → dulu menang otomatis di cek pertama).
        dist = max(abs(p["target_price"] / p["entry_price"] - 1) * 100, ADVERSE_FLOOR_PCT)
        favor = actual_pct if p["direction"] == "UP" else -actual_pct
        hit, adverse = favor >= dist, favor <= -dist
        if not hit and not adverse:
            continue
        outcome = "win" if hit else "loss"
        repo.resolve_prediction(p["id"], round(actual_pct, 2), outcome)
        _learn(p, actual_pct, outcome)
        repo.log("engine", "resolve",
                 f"{p['ticker']}: {f'searah {actual_pct:+.1f}% (>= {dist:.1f}%)' if hit else f'lawan arah {actual_pct:+.1f}% (tesis rusak)'} "
                 f"lebih awal -> {'menang' if hit else 'kalah'}, analisis ulang",
                 ticker=p["ticker"])
        n += 1
        try:
            # Analisis ulang DENGAN KONTEKS: agen tahu ia menilai kelanjutan gerakan yang sudah
            # terjadi (tren masih berbahan bakar atau exhausted?), bukan mulai dari nol.
            ctx = (f"Prediksi {p['direction']} sebelumnya {'MENCAPAI TARGET' if hit else 'GAGAL (lawan arah)'} "
                   f"lebih awal: harga bergerak {p['entry_price']}→{price} ({actual_pct:+.1f}%) "
                   f"dari horizon {p['horizon_days']} hari. Nilai sekarang: apakah pergerakan masih "
                   f"berlanjut (tren punya bahan bakar) atau sudah exhausted (rawan berbalik/datar)? "
                   f"Tentukan arah & target BARU dari harga sekarang.")
            run_one(p["ticker"], note=ctx)
        except Exception as e:  # noqa: BLE001
            repo.log("engine", "resolve", f"{p['ticker']}: gagal analisis ulang: {e}",
                     level="warn", ticker=p["ticker"])
    return n


def _finalize(p: dict, price: float) -> None:
    """Tutup 1 prediksi 'open' SEKARANG pakai harga saat ini: nilai menang/kalah seperti
    biasa (arah benar/salah dari pergerakan nyata), catat pelajaran. Sumber tunggal dipakai
    `resolve_due` (jatuh tempo wajar) DAN `_supersede_stale`/`_commit_1d` (digantikan
    analisis baru sebelum jatuh tempo — bukan "dibatalkan", tetap dinilai jujur)."""
    if not p.get("entry_price"):
        return
    actual_pct = (price / p["entry_price"] - 1) * 100
    outcome = "win" if is_win(p["direction"], actual_pct) else "loss"
    repo.resolve_prediction(p["id"], round(actual_pct, 2), outcome)
    _learn(p, actual_pct, outcome)


def resolve_due() -> int:
    """Evaluasi prediksi: (a) target SUDAH tercapai sebelum horizon habis -> tutup lebih
    awal sbg menang & analisis ULANG saham itu (lanjut atau balik arah?); (b) horizon
    sudah lewat tanpa target tercapai -> evaluasi arah seperti biasa."""
    now = datetime.now(timezone.utc)
    resolved = _resolve_target_hits()

    due = repo.open_predictions_due(now)
    for p in due:
        quote = repo.get_quote(p["ticker"])
        if not quote or not p["entry_price"]:
            continue
        _finalize(p, quote["price"])
        resolved += 1
    if resolved:
        repo.log("engine", "resolve", f"{resolved} prediksi dievaluasi & dipelajari")
    try:
        from app.agents import shadow
        shadow.resolve_due()
    except Exception as e:  # noqa: BLE001 — papan skor tak boleh menjatuhkan resolve produksi
        repo.log("shadow", "resolve", f"gagal: {e}", level="warn")
    return resolved


# Keyakinan ≥ ini + meleset = layak post-mortem (calon overconfidence / salah baca nyata).
HIGH_CONF_MISS = 65.0


def diagnose_miss(p: dict, actual_pct: float) -> str:
    """POST-MORTEM high-conf miss: kenapa meleset? sideways / reversal / berita penggerak.

    Hanya pakai data yang ANDAL saat resolve: (1) actual_pct → sideways vs lawan-arah;
    (2) berita di JENDELA prediksi (resolve jalan tepat saat horizon habis, jadi
    max_age_hours≈umur prediksi menangkap [dibuat..sekarang] = jendelanya). Sengaja TIDAK
    pakai lean makro 'sekarang' (≠ lean saat prediksi dibuat → menyesatkan).
    ponytail: heuristik kategori; upgrade ke post-mortem LLM bila perlu lebih dalam.
    """
    d = p["direction"]
    bits: list[str] = []
    if abs(actual_pct) < 1.0:
        bits.append(f"SIDEWAYS (gerak {actual_pct:+.1f}%): sinyal {d} tak berbuah & keyakinan "
                    f"{p['probability']:.0f}% kelewat tinggi utk gerakan datar — horizon "
                    f"{p['horizon_days']}h mungkin kependekan / sinyal sebetulnya lemah")
    elif (d == "UP" and actual_pct < 0) or (d == "DOWN" and actual_pct > 0):
        bits.append(f"REVERSAL lawan arah ({actual_pct:+.1f}%): tesis {d} salah baca")
    else:
        bits.append(f"arah benar tapi target meleset ({actual_pct:+.1f}%)")
    try:
        win_h = (int(p.get("horizon_days") or 3) + 1) * 24 + 12
        rows = repo.news_for_ticker(p["ticker"], limit=8, max_age_hours=win_h)
        # Utamakan berita yang BENAR-BENAR menyebut saham ini (ter-tag) — jangan menuding
        # berita makro/global generik (mis. "Bitcoin -4%") sbg penyebab miss saham kecil.
        spec = [n for n in rows if p["ticker"] in (n.get("tickers") or "")
                and abs(int(n.get("impact") or 0)) >= 2]
        if spec:
            n0 = max(spec, key=lambda n: abs(int(n.get("impact") or 0)))
            bits.append(f"berita SPESIFIK [{n0['sentiment']} {int(n0['impact']):+d}]: {n0['title'][:75]}")
        else:
            glob = [n for n in rows if abs(int(n.get("impact") or 0)) >= 3]
            if glob:
                n0 = max(glob, key=lambda n: abs(int(n.get("impact") or 0)))
                bits.append(f"konteks global (bukan spesifik {p['ticker']}): {n0['title'][:65]}")
    except Exception:  # noqa: BLE001
        pass
    return " | ".join(bits)


def _learn(p: dict, actual_pct: float, outcome: str) -> None:
    """Tulis pelajaran ringkas dari hasil — diinject ke prompt siklus berikutnya."""
    dir_txt = p["direction"]
    if outcome == "win":
        lesson = (f"Prediksi {dir_txt} {p['ticker']} BENAR (aktual {actual_pct:+.1f}% "
                  f"dalam {p['horizon_days']}h, keyakinan {p['probability']}%). "
                  f"Pola/alasan: {(p.get('reasoning') or '')[:120]}")
        kind = "win"
    else:
        # High-conf miss → post-mortem tajam (sideways/reversal/berita) untuk diperbaiki agen.
        if (p.get("probability") or 0) >= HIGH_CONF_MISS:
            lesson = (f"MELESET KEYAKINAN TINGGI: {dir_txt} {p['ticker']} klaim "
                      f"{p['probability']:.0f}% → aktual {actual_pct:+.1f}%. "
                      f"POST-MORTEM: {diagnose_miss(p, actual_pct)}. "
                      f"Sinyal asal: {(p.get('reasoning') or '')[:80]}")
            kind = "loss-highconf"
        else:
            lesson = (f"Prediksi {dir_txt} {p['ticker']} MELESET (aktual {actual_pct:+.1f}% "
                      f"vs ekspektasi {p.get('expected_pct')}%). Tinjau ulang sinyal: "
                      f"{(p.get('reasoning') or '')[:120]}")
            kind = "loss"
    repo.add_lesson(kind, lesson, ticker=p["ticker"], prediction_id=p["id"])
