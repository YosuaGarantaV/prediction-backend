"""Sistem 'naik level' agen — kalibrasi data-driven yang ANTI-OVERFIT.

Tujuan: bikin agen makin akurat TANPA overfit ke kebetulan/noise. Caranya:
  - Shrinkage: win-rate sampel kecil ditarik ke base-rate 50% (Bayesian smoothing),
    jadi pola dgn sedikit data tidak dianggap "hebat" hanya karena beruntung.
  - Sample gating: angka pola hanya ditampilkan kalau jumlah sampel cukup.
  - Validasi out-of-sample dari backtest (bukan cuma in-sample).
  - Brier score: ukur seberapa jujur kalibrasi probabilitasnya.

Output `calibration_guidance()` diinject ke prompt agen agar mereka menyesuaikan
tingkat keyakinan ke realisasi historis — bukan menebak overconfident.
"""
from __future__ import annotations

import time

from app import backtest, repo

SHRINK_K = 12     # kekuatan tarikan ke base-rate (makin besar = makin konservatif)
MIN_SAMPLES = 20  # minimum sampel agar pola dianggap informatif

# Berapa banyak SEBARAN keyakinan mentah yang dipertahankan setelah dipusatkan ke realisasi.
# 0 = perilaku lama (semua saham dapat angka identik). 1 = sebaran mentah utuh. 0,5 dipilih
# karena skor mentah TERUKUR tak punya edge (app.eval 2026-08-31: edge -0,158%, CI memuat 0)
# → sebarannya pantas diredam, tapi tidak dinolkan, karena menolkannya justru yang mematikan
# seluruh mesin (lihat docstring calibrated_probability).
# ponytail: satu konstanta; ganti jadi hasil fit begitu `raw_prob` sudah terarsip cukup lama
# (collector `raw_probability` di repo.save_prediction, mulai 2026-08-31).
KEEP_SPREAD = 0.5


def _shrunk_winrate(wins: int, n: int, base: float = 0.5) -> float:
    """Win-rate dengan smoothing Bayesian ke base-rate (hindari overfit sampel kecil)."""
    return round((wins + SHRINK_K * base) / (n + SHRINK_K) * 100, 1)


def calibrated_probability(direction: str, raw_prob: float,
                           keep_spread: float | None = None) -> float:
    """Geser keyakinan FORMULA → realisasi historis NYATA, TANPA meratakan semua saham.

    Makin banyak outcome terkumpul, makin condong ke win-rate yang BENAR-BENAR terjadi untuk
    arah itu. Aman saat data minim (shrinkage ke base-rate + blend bertahap).

    PERBAIKAN 2026-08-31 — versi lama MENGGANTI nilai, bukan menggeser. Pada n>=30 bobot
    `w` mencapai 1,0 sehingga `raw_prob` dibuang total dan yang keluar cuma `realized`: SATU
    skalar per arah, sama persis untuk setiap saham hari itu. Terukur di 200 prediksi live
    30 Agt: seluruh lengan UP hanya punya dua nilai keyakinan (58,4 dan 58,9), lengan DOWN
    dua nilai (38,1 dan 38,2), FLAT terpaku 53,0. Karena setiap gerbang aksi membandingkan
    angka ini ke ambang TETAP, hasilnya bukan penilaian per-saham melainkan sakelar ON/OFF
    global: 24-31 Agt lengan UP duduk di 58,4 < lantai 60 → NOL pembelian 5 hari bursa dan
    portofolio 100% kas, sementara yang berubah cuma rata-rata bergerak 500 prediksi.
    Dari 970 prediksi DOWN sepanjang riwayat cuma 50 yang pernah melewati 66, jadi gerbang
    rezim "DOWN lemah → FLAT" praktis selalu menyala.

    Sekarang realisasi dipakai sebagai PUSAT, dan jarak sinyal ini dari netral (raw-50)
    dipertahankan sebesar KEEP_SPREAD di sekitarnya. Rata-rata tetap jujur ke realisasi
    (klaim agregat tak jadi overconfident) tapi dua saham dengan skor berbeda tak lagi
    keluar dengan angka yang sama.

    `keep_spread=0` mengembalikan perilaku lama (semua rata) dan itu YANG BENAR untuk klaim
    LLM: sebaran hanya layak dipertahankan kalau ia membawa informasi, dan klaim CTO terukur
    TIDAK (audit 2026-07-08, n=1.532: klaim 70-80% realisasi 47,1% sementara klaim 60-70%
    justru 53,3% — urutannya terbalik). Jalur heuristik memakai default karena `raw_prob`-nya
    turunan deterministik dari skor, bukan angka yang dikarang model bahasa.
    """
    if keep_spread is None:
        keep_spread = KEEP_SPREAD
    if direction == "FLAT":
        return round(raw_prob, 1)
    res = [r for r in repo.resolved_predictions(500) if r["direction"] == direction]
    n = len(res)
    if n < 8:
        # Data arah ini minim → JANGAN percaya formula mentah (lubang nyata 2026-07-17:
        # DB baru 2 hari, UP resolved n=6 → klaim heuristik 74% lolos TANPA kalibrasi
        # → tembus re-gate BUY>=60 → 6x BUY JELI; realisasi arah gabungan cuma 24%).
        # Fallback: pakai gabungan UP+DOWN sbg prior (perilaku arah berkorelasi di rezim
        # sama); dua-duanya minim (<8) → baru pakai formula apa adanya (sistem benar2 baru).
        res = [r for r in repo.resolved_predictions(500) if r["direction"] in ("UP", "DOWN")]
        n = len(res)
        if n < 8:
            return round(raw_prob, 1)
    wins = sum(1 for r in res if r["outcome"] == "win")
    realized = _shrunk_winrate(wins, n)         # win-rate historis (sudah di-smoothing)
    # NEXT_UPDATES item-2 (PASS 2026-06-29: UP 26/101=26% lolos bar n>=40 & wr<40%):
    # shrink lebih cepat (60→30 outcome utk 100% percaya realisasi) + lantai turun 35→30 supaya
    # arah TERBUKTI lemah (UP) tercermin jujur, bukan ditahan artifisial di 35%.
    w = min(1.0, n / 30.0)                       # 30 outcome → 100% percaya realisasi (dulu 60)
    anchored = realized + (raw_prob - 50.0) * keep_spread   # pusat = realisasi, sebaran dijaga
    return round(max(30.0, min(85.0, raw_prob * (1 - w) + anchored * w)), 1)


_ARM_CACHE: dict = {}
ARM_TTL_S = 3600.0     # vonis lengan bergerak lambat (unit bukti = TANGGAL); 1 jam cukup


def arm_verdict(direction: str, min_horizon: int = 2, side: str = "below") -> dict:
    """Vonis satu LENGAN taruhan (arah × horizon>=min_horizon) terhadap base rate pasar.

    `side="below"` → `pass=True` berarti lengan TERBUKTI di bawah base rate (dipakai
    `_proven_loss_gate`: berhenti bertaruh). `side="above"` → `pass=True` berarti lengan
    TERBUKTI di atas base rate (dipakai gerbang uang: baru boleh membelanjakan kas).
    Keduanya menuntut CI 95% bootstrap blok per-TANGGAL seluruhnya di sisi yang diuji, bukan
    sekadar titik estimasi. Di-cache ARM_TTL_S detik (bootstrap 3000 iterasi terlalu mahal
    untuk dipanggil tiap commit).
    """
    key = (direction, min_horizon, side)
    hit = _ARM_CACHE.get(key)
    if hit and (time.time() - hit[0]) < ARM_TTL_S:
        return hit[1]
    from app.eval import baseline_for, measured_baseline, verdict_winrate
    pool = [r for r in repo.resolved_predictions(500)
            if (r.get("horizon_days") or 3) >= min_horizon]
    res = [r for r in pool
           if r["direction"] == direction and r.get("outcome")
           and (r.get("resolved_at") or r.get("ts"))]
    if not res:
        out = {"pass": False, "detail": "belum ada outcome di lengan ini"}
    else:
        # Base rate DIUKUR di jendela yang sama — lihat eval.measured_baseline untuk bukti
        # kenapa tabel statis meloloskan vonis palsu di KEDUA arah (audit 2026-09-08).
        # Tabel tinggal cadangan saat sampel tipis, dan sumbernya ikut ditulis ke `detail`.
        base, src = measured_baseline(direction, pool), "diukur"
        if base is None:
            base = sum(baseline_for(r["direction"], r.get("horizon_days") or 3)
                       for r in res) / len(res)
            src = "tabel (sampel tipis)"
        recs = [{"ts": (r.get("resolved_at") or r.get("ts"))[:10],
                 "win": r["outcome"] == "win"} for r in res]
        try:
            out = verdict_winrate(recs, base, side=side)
            out["baseline_source"] = src
            out["detail"] = f"{out.get('detail', '')} | base rate {src} (n={len(pool)})"
        except Exception:  # noqa: BLE001 — vonis gagal tak boleh memblokir prediksi
            out = {"pass": False, "detail": "vonis lengan gagal dihitung"}
    _ARM_CACHE[key] = (time.time(), out)
    return out


def _brier(recs: list[dict]) -> float:
    if not recs:
        return 0.0
    s = sum((r["probability"] / 100 - (1 if r["outcome"] == "win" else 0)) ** 2
            for r in recs)
    return round(s / len(recs), 3)


def agent_skill() -> dict:
    """Metrik skill agregat + level. Menggabungkan hasil LIVE & backtest OOS.

    LEVEL & win-rate = keterampilan ARAH pada TARUHAN NYATA (UP/DOWN, horizon>=2). Dua hal
    DIKELUARKAN dari level (tetap dievaluasi & tampil di statistik lain, hanya tak menghukum
    SKILL-ARAH):
      - FLAT = "tak ada sinyal", bukan taruhan berarah (loophole 2026-07-13: FLAT @15.9%
        menyeret level ke Lv.2 artifisial; sudah tak dibuat sejak 2026-07-07).
      - Companion horizon=1 = FORECAST TAMPILAN "naik/turun besok", TIDAK memicu trade & paling
        bising (audit 2026-07-15: 1h menang 29.0% vs bet multi-hari 50.9%; 7% sampel ini
        menyeret agregat 50.9%→49.3% = melintasi ambang 'di bawah acak' secara artifisial).
        Sama kelas dgn bug FLAT: metrik headline dicemari item non-taruhan."""
    allres = repo.resolved_predictions(500)
    res = [r for r in allres
           if r["direction"] in ("UP", "DOWN") and (r.get("horizon_days") or 3) >= 2]
    n = len(res)
    wins = sum(1 for r in res if r["outcome"] == "win")
    raw = round(wins / n * 100, 1) if n else 0.0
    calibrated = _shrunk_winrate(wins, n)
    brier = _brier(res)

    bt = backtest.load_result()
    oos = bt.get("oos_win_rate", 0.0)

    base_live = market_baseline_live(res)
    # backtest.json lama belum punya "baseline" — hitung dari komposisinya, jangan diam-diam
    # jatuh ke 50 (itu justru bug yang sedang diperbaiki).
    base_bt = bt.get("baseline") or backtest.market_baseline(
        bt.get("horizon") or 5, bt.get("up_n") or 0, bt.get("down_n") or 0)
    edge_live = round(calibrated - base_live, 1) if n else 0.0
    edge_oos = round(oos - base_bt, 1) if oos else None
    proven = _proven_above(res, base_live)

    level, label = _level(n, edge_live, edge_oos, bt.get("overfit_gap"), proven.get("pass"))
    return {
        "resolved": n, "raw_win_rate": raw, "calibrated_win_rate": calibrated,
        "brier": brier, "backtest_oos": oos,
        # pembanding yang benar: base rate pasar, bukan 50% (lihat backtest.market_baseline)
        "baseline_live": base_live, "baseline_backtest": base_bt,
        "edge_live": edge_live, "edge_backtest": edge_oos,
        "edge_proven": bool(proven.get("pass")), "edge_evidence": proven.get("detail", ""),
        # transparansi: berapa yg TAK dihitung ke level (FLAT + companion 1-hari)
        "excluded": len(allres) - n,
        "flat_excluded": len(allres) - n,   # alias lama (kompat frontend/report)
        "backtest_verdict": bt.get("verdict", "(backtest belum dijalankan)"),
        "level": level, "level_label": label,
        # Status model statistik DI PERMUKAAN. Sejak 13 Agt 2026 model.json ber-AUC 0,451
        # (di bawah acak) sehingga `is_usable` membungkamnya: gate model-agreement, pilar
        # bukti UP, dan pemeringkatan scan semuanya mati DIAM-DIAM sementara komentar di
        # kode masih menyebut "AUC 0,62". Dashboard harus bisa melihat sendiri.
        "model_status": _model_status(),
        # Vonis per-LENGAN (dipakai _proven_loss_gate). Lengan ber-`pass`=True sedang ditutup.
        "arm_verdicts": {d: arm_verdict(d, 2).get("detail", "") for d in ("UP", "DOWN")},
    }


def _model_status() -> dict:
    """Apakah model statistik benar-benar dipakai sekarang? (bukan menurut komentar kode)"""
    try:
        import json as _json

        from app import model as _m
        if not _m.MODEL_FILE.exists():
            return {"usable": False, "reason": "model.json belum ada"}
        m = _json.loads(_m.MODEL_FILE.read_text(encoding="utf-8"))
        ok = _m.is_usable(m)
        return {"usable": bool(ok), "oos_auc": m.get("oos_auc"), "edge": m.get("edge"),
                "min_auc": _m.MIN_USABLE_AUC, "trained_n": m.get("n"),
                "reason": "" if ok else (f"OOS AUC {m.get('oos_auc')} < {_m.MIN_USABLE_AUC} — "
                                         f"DIBUNGKAM, tak menyetir keputusan apa pun")}
    except Exception:  # noqa: BLE001
        return {"usable": False, "reason": "status model gagal dibaca"}


def market_baseline_live(res: list[dict]) -> float:
    """Base rate pasar untuk komposisi arah+horizon yang BENAR-BENAR dipertaruhkan live.

    Dasarnya `eval.measured_baseline` (diukur di baris yang sama) supaya angka di dashboard
    dan angka yang dipakai gerbang uang berasal dari mistar yang sama — sebelum 2026-09-08
    keduanya memakai tabel statis, dan tabel itu menggelembungkan edge ~9 pp di arah UP.
    Tabel jadi cadangan untuk sampel tipis."""
    from app.eval import baseline_for, measured_baseline   # lazy: eval menarik numpy/pandas
    if not res:
        return 50.0
    meas = {d: measured_baseline(d, res) for d in ("UP", "DOWN")}
    return round(sum(meas.get(r["direction"]) if meas.get(r["direction"]) is not None
                     else baseline_for(r["direction"], r.get("horizon_days") or 3)
                     for r in res) / len(res), 1)


def _proven_above(res: list[dict], base: float) -> dict:
    """Gerbang bukti: edge live TERBUKTI di atas base rate? Unit = TANGGAL, bukan baris.

    11 ribu baris backtest hanya bernilai ~220 pengamatan bebas (return tumpang tindih +
    saham berkorelasi di tanggal sama), jadi titik estimasi positif saja tak cukup untuk
    menaikkan level. Tanpa gerbang ini perbaikan base rate cuma menukar label salah
    "di bawah acak" dengan label salah "Menengah".
    """
    from app.eval import verdict_winrate
    # tanpa tanggal sebuah outcome tak bisa jadi unit bukti — dibuang, bukan bikin crash
    recs = [{"ts": (r.get("resolved_at") or r.get("ts") or "")[:10],
             "win": r["outcome"] == "win"}
            for r in res if r.get("outcome") and (r.get("resolved_at") or r.get("ts"))]
    try:
        return verdict_winrate(recs, base, side="above")
    except Exception:  # noqa: BLE001 — gerbang bukti tak boleh menjatuhkan /api/skill
        return {"pass": False, "detail": "gerbang bukti gagal dihitung"}


def _level(n: int, edge_live: float, edge_oos, gap, proven: bool) -> tuple[int, str]:
    """Level = EDGE di atas base rate pasar (bukan di atas 50% — lihat backtest.market_baseline)."""
    if n < 30 and edge_oos is None:
        return 1, "Pemula (data belum cukup untuk dinilai)"
    # Tanpa track record live yang cukup, andalkan backtest OOS (jujur, bukan default 0).
    edge = edge_oos if n < 30 else max(edge_live, edge_oos if edge_oos is not None else edge_live)
    if gap is not None and gap > 12:
        return 2, "Hati-hati (tanda overfit: out-of-sample lemah)"
    if edge < 0:
        return 2, f"Lemah ({edge:+.1f} pp di bawah base rate pasar)"
    if not proven:
        return 3, f"Dasar (edge {edge:+.1f} pp, belum signifikan per-tanggal)"
    if edge < 7:
        return 4, f"Menengah (edge {edge:+.1f} pp, terbukti)"
    if edge < 12:
        return 5, f"Mahir (edge {edge:+.1f} pp, terbukti)"
    return 6, f"Ahli (edge {edge:+.1f} pp di atas base rate, terbukti)"


def performance_digest(days: int = 14) -> str:
    """Digest performa N hari terakhir — bekal BELAJAR minggu agent (AGENT_MODE): agen membaca
    rekam jejak pipeline 2 minggu (win-rate per arah, kesalahan berulang) sebelum memutuskan.
    Ringkas (~15 baris); '' bila belum ada data (aman di DB kosong)."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = repo.get_conn()
    # SATU definisi resolusi sah, sama dengan calibrated_probability/agent_skill: taruhan
    # NYATA saja. Query mentah `status='resolved'` di sini memakai populasi TERCEMAR —
    # diukur 2026-07-30 di live: mentah n=423 win 46,8% vs bersih n=112 win 43,8% (73% baris
    # adalah forecast display 1-hari + resolusi belum lewat sesi bursa). Digest ini masuk
    # prompt agen + knowledge.auto_tune, jadi agen diberi tahu angka yang lebih bagus
    # daripada layar user — persis kesalahan yang diperbaiki utk TAMPILAN pada 27 Jul.
    rows = [r for r in repo.resolved_predictions(5000)
            if (r["resolved_at"] or "") >= cutoff]
    if not rows:
        return ""
    lines: list[str] = []
    n = len(rows)
    wins = sum(1 for r in rows if r["outcome"] == "win")
    lines.append(f"Total {days} hari: {wins}/{n} benar ({100*wins/n:.0f}%).")
    for d in ("UP", "DOWN", "FLAT"):
        sub = [r for r in rows if r["direction"] == d]
        if len(sub) >= 5:
            w = sum(1 for r in sub if r["outcome"] == "win")
            lines.append(f"Arah {d}: {w}/{len(sub)} benar ({100*w/len(sub):.0f}%).")
    hc = [r for r in rows if (r["probability"] or 0) >= 65 and r["outcome"] == "loss"]
    if hc:
        lines.append(f"Miss keyakinan tinggi (>=65%): {len(hc)}x — kalibrasikan turun.")
    # Kategori kesalahan berulang dari post-mortem (lessons loss-highconf)
    lrows = conn.execute("SELECT lesson FROM lessons WHERE kind='loss-highconf' AND ts>=?",
                         (cutoff,)).fetchall()
    cats = {"SIDEWAYS": 0, "REVERSAL": 0, "berita": 0}
    for r in lrows:
        for k in cats:
            if k in (r["lesson"] or ""):
                cats[k] += 1
    top = [f"{k.lower()} {v}x" for k, v in
           sorted(cats.items(), key=lambda kv: kv[1], reverse=True) if v][:2]
    if top:
        lines.append(f"Pola miss terbanyak: {', '.join(top)}.")
    return (f"REKAM JEJAK ENGINE {days} HARI TERAKHIR (belajar dari ini — jangan ulangi "
            f"kesalahan yang sama):\n- " + "\n- ".join(lines))


def calibration_guidance() -> str:
    """Teks kalibrasi untuk diinject ke prompt agen (membatasi overconfidence)."""
    res = repo.resolved_predictions(500)
    lines: list[str] = []

    if len(res) >= MIN_SAMPLES:
        for d in ("UP", "DOWN"):
            sub = [r for r in res if r["direction"] == d]
            if len(sub) >= MIN_SAMPLES:
                w = sum(1 for r in sub if r["outcome"] == "win")
                wr = _shrunk_winrate(w, len(sub))
                # SELALU sandingkan base rate: "UP menang 43%" terdengar buruk padahal base
                # rate UP cuma 38,7% (band +/-0,5% dihitung kalah). Tanpa pembanding ini agen
                # membaca angkanya sebagai gagal dan menekan keyakinannya sendiri.
                b = market_baseline_live(sub)
                lines.append(f"Sinyal {d}: realisasi menang {wr}% vs base rate pasar {b}% "
                             f"({wr - b:+.1f} pp, n={len(sub)}, sudah di-smoothing).")
        # Kalibrasi per bucket keyakinan
        for lo, hi in [(70, 101), (60, 70)]:
            b = [r for r in res if lo <= r["probability"] < hi]
            if len(b) >= MIN_SAMPLES:
                w = sum(1 for r in b if r["outcome"] == "win")
                real = _shrunk_winrate(w, len(b))
                lines.append(f"Saat keyakinan {lo}-{min(hi,100)}%, realisasi sebenarnya ~{real}% "
                             f"→ {'JANGAN overconfident' if real < lo else 'kalibrasi ok'}.")
        # Per horizon: jendela mana yang TERBUKTI bekerja (live). Audit 2026-07-08:
        # 3h 51.8% vs 5h 30.9% — pilih horizon dari data, bukan kebiasaan.
        # Bandingkan EDGE, bukan win-rate mentah: band +/-0,5% memakan 27,8% gerak di h=1 tapi
        # cuma 14,6% di h=5, jadi win-rate mentah SELALU menanjak dengan horizon walau sinyalnya
        # sama kuat. Memilih horizon dari angka mentah = memilih artefak scoring.
        hz = []
        for h in (1, 3, 5, 7):
            sub = [r for r in res if r["horizon_days"] == h]
            if len(sub) >= MIN_SAMPLES:
                w = sum(1 for r in sub if r["outcome"] == "win")
                wr = _shrunk_winrate(w, len(sub))
                hz.append(f"{h}h={wr - market_baseline_live(sub):+.1f} pp (n={len(sub)})")
        if hz:
            lines.append("Edge per horizon vs base rate pasar: " + ", ".join(hz) +
                         " → pakai horizon terbaik kecuali ada katalis bertanggal.")

    bt = backtest.load_result()
    if bt.get("total"):
        lines.append(f"Backtest out-of-sample: {bt.get('oos_win_rate')}% "
                     f"(Brier {bt.get('brier')}). {bt.get('verdict','')}")
        for c in bt.get("calibration", []):
            if c["n"] >= MIN_SAMPLES:
                lines.append(f"  • klaim ~{int(c['claimed'])}% → realisasi {c['realized']}% "
                             f"(n={c['n']})")

    if not lines:
        return ("Belum ada cukup data historis untuk kalibrasi. Bersikap konservatif: "
                "jangan beri keyakinan >65% tanpa konfirmasi kuat.")
    return ("KALIBRASI HISTORIS (sesuaikan keyakinanmu ke angka nyata ini, hindari "
            "overconfident & overfit):\n- " + "\n- ".join(lines))
