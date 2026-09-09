"""Faktor prediksi STRUKTURAL / EVENT — BUKAN harga-TA (yang sudah terbukti ~acak di universe
ini, korelasi ~0). Tiap faktor punya NAMA STABIL → dicatat per-prediksi → win-rate-nya
dievaluasi di laporan harian ('mana faktor yang benar-benar bekerja').

LIVE-ONLY: dipakai di heuristic.decide() & prompt LLM, TIDAK di score_signals() supaya
backtest tetap deterministik tanpa look-ahead (pola sama dgn macro_lean).
"""
from __future__ import annotations

import json
import statistics

from app import repo

# RANTAI TRANSMISI komoditas/kurs → emiten (Aturan 11-18/67 DIEKSEKUSI deterministik, bukan
# hanya diceritakan ke LLM). Kunci = nama di repo.all_macro() (refresh 30 mnt). slug = nama
# faktor stabil utk scoreboard. Gerak komoditas >= LEAD_MOVE_PCT → sinyal berarah utk saham
# yang tersambung; komoditas NAIK selalu positif utk daftar ini (produsen/eksportir).
_COMMODITY_MAP = {
    "WTI Crude Oil": ("minyak", ("MEDC", "PGAS", "ELSA", "ENRG", "RAJA", "AKRA")),
    "Gold": ("emas", ("ANTM", "MDKA", "HRTA", "PSAB", "EMAS", "BRMS")),
    "Copper": ("tembaga", ("AMMN", "MDKA")),
    # proxy batu bara = harga ITMG sendiri → ITMG DIKECUALIKAN (hindari sirkular)
    "Coal (proxy ITMG)": ("batubara", ("ADRO", "PTBA", "INDY", "HRUM", "ADMR", "BUMI", "DOID", "PTRO")),
    # rupiah lemah (USD/IDR naik) = positif eksportir komoditas (Aturan 15)
    "USD/IDR": ("usdidr", ("ADRO", "PTBA", "ITMG", "INDY", "HRUM", "AALI", "LSIP", "SSMS", "DSNG", "TAPG")),
}
LEAD_MOVE_PCT = 2.0   # gerak komoditas < ini = bukan katalis berarti


def _commodity_lead(ticker: str, feats: dict) -> list[dict]:
    """Faktor lead-lag: komoditas penggerak sektor bergerak BESAR → saham tersambung menyusul.
    Anti priced-in: saham yang SUDAH lari searah >=5%/3hr dianggap sudah menyerap katalis."""
    out: list[dict] = []
    try:
        rows = {r["name"]: (r["change_pct"] or 0.0) for r in repo.all_macro()}
    except Exception:  # noqa: BLE001
        return out
    cum3 = float(feats.get("cum_change_3d") or 0.0)
    for name, (slug, tickers) in _COMMODITY_MAP.items():
        if ticker not in tickers:
            continue
        chg = rows.get(name)
        if chg is None or abs(chg) < LEAD_MOVE_PCT:
            continue
        d = 1 if chg > 0 else -1
        if d * cum3 >= 5:
            continue  # sudah bergerak searah — katalis kemungkinan priced-in (Aturan 78)
        out.append({"name": f"lead_{slug}", "dir": d, "weight": 0.5,
                    "note": f"{name} {chg:+.1f}% → transmisi ke {ticker} (Aturan 67)"})
    return out


def _sector_divergence(ticker: str, feats: dict) -> list[dict]:
    """RELATIVE VALUE: saham menyimpang >2 sigma dari gerak 3-hari SEKTORNYA tanpa berita
    spesifik = kemungkinan noise → rawan konvergensi balik ke sektor. Dengan berita spesifik
    (|impact|>=2) divergensi dianggap beralasan → tidak difade. Hanya peer likuid (>Rp1M)."""
    try:
        conn = repo.get_conn()
        row = conn.execute("SELECT sector FROM fundamentals WHERE ticker=?", (ticker,)).fetchone()
        if not row or not row["sector"]:
            return []
        peers = conn.execute(
            "SELECT q.features_json FROM fundamentals f JOIN quotes q ON q.ticker=f.ticker "
            "WHERE f.sector=? AND f.ticker!=? AND q.price*q.volume > 1e9 LIMIT 25",
            (row["sector"], ticker)).fetchall()
        vals = []
        for p in peers:
            try:
                vals.append(float(json.loads(p["features_json"] or "{}").get("cum_change_3d") or 0.0))
            except Exception:  # noqa: BLE001
                continue
        if len(vals) < 6:
            return []
        mu, sd = statistics.mean(vals), statistics.pstdev(vals)
        if sd < 0.5:
            return []
        me = float(feats.get("cum_change_3d") or 0.0)
        z = (me - mu) / sd
        if abs(z) < 2.0:
            return []
        spec = [n for n in repo.news_for_ticker(ticker, limit=6, max_age_hours=48)
                if ticker in (n.get("tickers") or "") and abs(int(n.get("impact") or 0)) >= 2]
        if spec:
            return []  # ada katalis nyata → divergensi beralasan, jangan difade
        return [{"name": "divergen_sektor", "dir": -1 if z > 0 else 1, "weight": 0.5,
                 "note": f"gerak 3h {me:+.1f}% vs sektor {mu:+.1f}% (z={z:+.1f}) tanpa "
                         f"berita — rawan konvergensi balik"}]
    except Exception:  # noqa: BLE001
        return []


# Judul filing/berita ter-tag emiten → aksi korporasi berarah (struktural, bukan sekadar sentimen).
_BULL = {
    "buyback": "buyback", "pembelian kembali saham": "buyback",
    "dividen": "dividen", "cum date": "dividen",  # apa pun ttg dividen → faktor mekanis (Aturan 73)
    "stock split": "split", "akuisisi": "akuisisi",
    "kontrak baru": "kontrak", "tender offer": "tender_offer",
}
_BEAR = {
    "rights issue": "rights_issue", "hmetd": "rights_issue", "private placement": "dilusi",
    "uma": "uma", "unusual market activity": "uma", "suspensi": "suspensi",
    "disuspensi": "suspensi", "gagal bayar": "gagal_bayar", "pkpu": "pkpu",
    "pailit": "pailit", "penurunan laba": "laba_turun", "rugi bersih": "rugi",
}


def extra_signals(ticker: str, feats: dict, price: float) -> list[dict]:
    """Faktor struktural/event yang fired untuk saham ini SEKARANG. Tiap: {name,dir,weight,note}.
    dir +1 bullish / -1 bearish; weight = pengaruh ke skor; name = kunci stabil utk evaluasi."""
    out: list[dict] = []

    # 1. ARUS ASING RESMI + BUKU ORDER penutupan. Satu payload IDX, jadi dinilai BERSAMA.
    #
    #    Diukur 2026-09-06 atas arsip 394 hari bursa yang baru terisi (data/foreign_flow/,
    #    `tools/eval_foreign_archive.py`): entry di OPEN t+1 (IDX terbit sesudah closing —
    #    entry-close = look-ahead senilai 4 pp), base rate dicocokkan tanggal dari sampel
    #    yang sama, CI blok `app.eval.block_bootstrap`. Sendiri-sendiri keduanya tipis:
    #    fp >= +0,40 memberi +2,5 pp dan buku >= +0,6 memberi +2,3 pp (h5). BERBARENGAN
    #    hasilnya di atas jumlah keduanya — +8,1 / +5,6 / +6,2 pp di h1/h3/h5, batas bawah
    #    CI > 0 di ketiganya (n=1.680), dan lulus terpisah di 2025 (+8,8/+5,8/+4,7) maupun
    #    2026 (+7,1/+5,2/+8,5). Karena itu kasus gabungan keluar sebagai SATU faktor
    #    bernama; menumpuk dua bobot lama (0,6+0,5) menghitung bukti yang sama dua kali.
    #
    #    JUJUR SOAL UANG: yang terbukti naik hanya AKURASI. Net sesudah fee 0,40% = +0,77%
    #    (h5, seluruh sampel) tapi pecah per tahun berbalik tanda: 2025 +1,63% vs 2026
    #    -0,79%. Menang lebih sering, menang lebih kecil. Jangan dibaca sebagai mesin cuan.
    #
    #    Sisi DOWN SENGAJA tidak digabung: gerbang cerminnya lulus hanya di h3 (+3,7 pp),
    #    h1 dan h5 gagal. Belum cukup bukti, jadi dua faktor lama tetap dipakai apa adanya.
    #
    #    Bobot 1,2 sengaja LEBIH RENDAH dari skala edge-nya (fp 0,6 utk +2,5 pp -> gabungan
    #    +6,2 pp setara ~1,4). Kalau live membenarkannya, `muted_factors` yang menaikkan
    #    lewat papan skor; menebak bobot ke atas duluan = mendahului bukti.
    try:
        from app.data import idxflow
        fp = idxflow.foreign_pressure(ticker)
        imb = idxflow.book_imbalance(ticker)
        _f = idxflow.foreign_for(ticker) or {}
        # Buku tipis = derau. Ambang antrean Rp200 juta ini SUDAH ikut diuji (predikat yang
        # diukur = persis baris di bawah), jangan dilonggarkan tanpa mengukur ulang.
        thick = ((_f.get("bidv") or 0) + (_f.get("offerv") or 0)) * price >= 2e8
        if fp is not None and imb is not None and fp >= 0.4 and imb >= 0.6 and thick:
            out.append({"name": "asing_beli_buku_tebal", "dir": +1, "weight": 1.2,
                        "note": f"asing net-BELI ({fp:+.2f}) DAN antrean beli dominan di tutup "
                                f"({imb:+.2f}) — gerbang gabungan, +6,2 pp di h5"})
        else:
            if fp is not None:
                if fp <= -0.4:
                    out.append({"name": "asing_jual", "dir": -1, "weight": 0.6,
                                "note": f"asing net-JUAL kuat ({fp:+.2f})"})
                elif fp >= 0.4:
                    out.append({"name": "asing_beli", "dir": +1, "weight": 0.6,
                                "note": f"asing net-BELI kuat ({fp:+.2f})"})
            if imb is not None and thick:
                if imb >= 0.6:
                    out.append({"name": "book_bid_heavy", "dir": +1, "weight": 0.5,
                                "note": f"antrean beli dominan di tutup (imbalance {imb:+.2f})"})
                elif imb <= -0.6:
                    out.append({"name": "book_offer_heavy", "dir": -1, "weight": 0.5,
                                "note": f"antrean jual dominan di tutup (imbalance {imb:+.2f})"})
    except Exception:  # noqa: BLE001
        pass

    # 1d. LEAD-LAG KOMODITAS + RELATIVE VALUE SEKTOR (rantai transmisi & konvergensi —
    #     matematika relasi, bukan TA absolut yang terbukti ~acak). Terukur di scoreboard.
    out.extend(_commodity_lead(ticker, feats))
    out.extend(_sector_divergence(ticker, feats))

    # 1b. IPO BARU (days_listed<=20) — pola Aturan 75, terkonfirmasi live 2026-07-10:
    #     JELI 2xARA lalu -14.8%, JECX -11.4%, BACH -9.8% (float kecil: euforia → distribusi
    #     tajam begitu streak putus). Faktor bernama → win-rate terukur di scoreboard.
    days = int(feats.get("days_listed") or 999)
    if days <= 20:
        chg = float(feats.get("change_pct") or 0.0)
        ara5 = int(feats.get("ara_days_5") or 0)
        arb5 = int(feats.get("arb_days_5") or 0)
        if arb5 >= 1 or chg <= -19:
            out.append({"name": "ipo_arb", "dir": -1, "weight": 1.0,
                        "note": f"IPO {days} hari kena ARB ({chg:+.1f}%) — bukan diskon, "
                                f"float kecil rawan lanjut jatuh"})
        elif ara5 >= 1 and chg < -5:
            out.append({"name": "ipo_distribusi", "dir": -1, "weight": 1.0,
                        "note": f"IPO {days} hari: streak ARA putus, hari merah {chg:+.1f}% "
                                f"= sinyal distribusi (Aturan 75)"})
        elif ara5 >= 2 and chg >= 0:
            out.append({"name": "ipo_euforia", "dir": -1, "weight": 0.3,
                        "note": f"IPO {days} hari masih euforia ARA {ara5}x — jangan kejar, "
                                f"rawan puncak"})

    # 2. AKSI KORPORASI / DIVIDEN dari filing IDX + berita ter-tag emiten (72 jam).
    #    Dividen = MEKANIS (bobot kecil, rawan salah-baca ex-date sbg bearish — Aturan 73).
    try:
        seen: set[str] = set()
        for n in repo.news_for_ticker(ticker, limit=12, max_age_hours=72):
            title = (n.get("title") or "").lower()
            for kw, name in _BULL.items():
                if kw in title and name not in seen:
                    seen.add(name)
                    w = 0.3 if name == "dividen" else 0.5
                    out.append({"name": f"ca_{name}", "dir": +1, "weight": w,
                                "note": f"aksi korporasi (+): {name}"})
            for kw, name in _BEAR.items():
                if kw in title and name not in seen:
                    seen.add(name)
                    out.append({"name": f"ca_{name}", "dir": -1, "weight": 0.7,
                                "note": f"risiko korporasi (-): {name}"})
    except Exception:  # noqa: BLE001
        pass

    return out


# --- AUTO-MUTE faktor merugikan (audit 2026-07-14): scoreboard menunjukkan mayoritas faktor
# KONSISTEN jauh di bawah base-rate (asing_jual 24.7%, ca_akuisisi 16.7%, book_offer_heavy
# 12.5% vs base 44.5%) — pola lebih mirip sinyal salah-arah daripada noise. Faktor yang
# (dengan shrinkage) tetap >8pt di bawah base DINOLKAN bobotnya, TAPI tetap dicatat ke
# `signals` → scoreboard terus mengumpulkan data → faktor pulih otomatis bila membaik.
# SENGAJA tidak dibalik arah: n masih kecil (8-121), flip di atas noise = overfit.
_MUTE_SHRINK_K = 12      # tarikan ke 50% (selaras skill.SHRINK_K)
_MUTE_MARGIN = 8.0       # harus 8pt di bawah base-rate baru dibisukan
_MUTE_MIN_N = 8          # minimal fired di prediksi resolved
_MUTE_TTL_S = 600
_mute_cache: dict = {"ts": 0.0, "muted": {}}


def muted_factors() -> dict[str, str]:
    """Peta {nama_faktor: 'win/n'} yang bobotnya dinolkan. Cache 10 mnt (scoreboard = scan
    semua prediksi resolved, jangan per-ticker per-siklus)."""
    import time
    now = time.time()
    if now - _mute_cache["ts"] < _MUTE_TTL_S:
        return _mute_cache["muted"]
    muted: dict[str, str] = {}
    try:
        st = repo.prediction_stats()
        resolved = (st.get("wins") or 0) + (st.get("losses") or 0)
        if resolved >= 100:                      # base-rate butuh sampel layak
            base = st["wins"] / resolved * 100
            for f in repo.factor_scoreboard(min_n=_MUTE_MIN_N):
                shrunk = (f["win"] + _MUTE_SHRINK_K * 0.5) / (f["n"] + _MUTE_SHRINK_K) * 100
                if shrunk < base - _MUTE_MARGIN:
                    muted[f["factor"]] = f"{f['win']}/{f['n']}"
    except Exception:  # noqa: BLE001 — DB bermasalah → tak ada yang dibisukan (perilaku lama)
        muted = {}
    _mute_cache.update(ts=now, muted=muted)
    return muted


def apply(ticker: str, feats: dict, price: float, score: float,
          factors: list[str]) -> tuple[float, list[str], list[dict]]:
    """Terapkan faktor struktural ke skor + catat. Return (skor_baru, factors_teks, signals).
    signals = daftar nama+dir yang FIRED → disimpan ke prediksi utk evaluasi win-rate per-faktor."""
    sigs = extra_signals(ticker, feats, price)
    muted = muted_factors()
    for s in sigs:
        wr = muted.get(s["name"])
        if wr is not None:
            factors.append(f"[{s['name']}] DIBISUKAN (win-rate {wr} jauh di bawah base) — "
                           f"{s['note']}")
            continue
        score += s["dir"] * s["weight"]
        factors.append(f"[{s['name']}] {s['note']}")
    return score, factors, sigs
