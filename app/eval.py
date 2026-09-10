"""Pengukuran jujur sebuah sinyal: BASELINE + bootstrap BLOK per-tanggal.

Kenapa modul ini ada (audit 2026-07-20): semua angka evaluasi di repo ini —
backtest.json, win-rate live, "forensik" yang melahirkan Aturan 26/71/79 — dihitung
TANPA dua hal, dan karenanya tak bisa ditafsirkan:

  1. BASELINE. "Win-rate 43%" terdengar buruk, padahal 46,5% gerak 5-hari di sampel
     ini memang turun >0,5%. Tanpa base rate, angka win-rate mengukur PASAR, bukan model.
  2. UNIT INDEPENDEN. Sampel harian tumpang-tindih (return 5-hari beririsan) dan
     berkorelasi lintas saham di tanggal sama. Rata-rata per-BARIS menganggap 9.421
     sampel independen padahal efektifnya ~59 tanggal → CI palsu sempit, ekor ekstrem
     beberapa saham illikuid menyetir seluruh kesimpulan.

Bukti bahwa ini bukan teori: desil-atas model tampak +1,95%/5hr (per-baris), tapi
+1,95% itu datang dari 87 sampel (9,2%) ber-return >+20% di saham turnover Rp0,07-2,59
juta/hari (BEEF +106%, BUKK +103%, BABY +96%) yang tak bisa dibeli dalam ukuran berarti.
Buang 87 ekor itu → -1,62%. Ditimbang per-tanggal → edge -0,33%, CI [-1,59%, +0,92%].

Pakai SEBELUM mempercayai sinyal apa pun. Sinyal lulus hanya kalau CI blok > 0.

CLI:  python -m app.eval
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from app import repo
from app.data.stocks import compute_features

# SATU sumber angka — jangan hardcode ulang di sini. `config.MIN_TURNOVER` bisa di-override
# lewat env; kalau modul ini punya salinan sendiri, gerbang bukti dan gate produksi bisa
# diam-diam memakai ambang berbeda.
MIN_TURNOVER = config.MIN_TURNOVER
FEE_ROUNDTRIP = (config.FEE_BUY + config.FEE_SELL) * 100  # % beli+jual round-trip

# BASE RATE pasar IDX — ambang menang |gerak| > 0,5% (gerak lebih kecil = KALAH, sama seperti
# scoring live), universe LIKUID saja (turnover >= MIN_TURNOVER). Dipakai sebagai pembanding
# gerbang bukti: "win-rate 43%" TAK berarti apa-apa tanpa ini.
#
# DIPERBARUI 2026-08-24 (`python -m app.eval --baseline`, n=16.281-16.537 sampel likuid per
# horizon). Angka lama (diukur 2026-07-20) berada 1,8-3,7 pp DI BAWAH angka ini di keenam sel:
#     lama  UP 32,9/37,2/38,7  DOWN 38,9/44,9/46,5   (h=1/3/5)
#     baru  UP 36,5/40,9/42,1  DOWN 42,6/47,0/48,3
# Artinya SELURUH klaim edge selama 5 minggu terakhir dihitung terhadap penyebut yang terlalu
# rendah → edge engine ter-OVERSTATE ~2-4 pp di mana-mana. Pembaruan ini membuat vonis lebih
# KETAT, bukan lebih ramah. Base rate juga sangat bergantung REZIM: diukur hanya atas sesi
# sejak 1 Jul 2026 (pasar menguat) angkanya UP 41,2/49,8/53,8 vs DOWN 37,2/37,5/36,9 — bergeser
# sampai 12 pp dan bertukar arah. Tabel ini sengaja SELURUH riwayat (konservatif & stabil);
# jangan diganti angka jendela-pendek tanpa alasan, itu mengejar rezim.
# ponytail: tetap tabel statis + refresh manual (desain asli: perubahan angka acuan lewat mata
# manusia). BASELINE_MEASURED disurfacing lewat agent_skill() supaya basi ketahuan sendiri.
BASELINE_MEASURED = "2026-08-24"
# Horizon panjang diukur 2026-09-10 dari 270 saham likuid, riwayat harga 2025-07-15..2026-09-09,
# ambang menang sama (|gerak| > 0,5%). Jumlah sampel ikut dicatat karena kepercayaannya jauh
# berbeda antar baris, dan tanpa itu angka 1 tahun terbaca sama otoritatifnya dengan angka 1 hari.
#
# PERINGATAN yang tak boleh dihapus: riwayat harga engine baru ~14 bulan. h=250 hanya punya 257
# sampel yang saling tumpang tindih di SATU jendela tahunan, jadi itu potret satu rezim, bukan
# base rate tahunan yang sah. h=60 dan h=120 juga menunjukkan DOWN 56-58%, artinya mayoritas
# saham likuid memang turun pada jendela 3-6 bulan periode ini. Perlakukan tiga baris terbawah
# sebagai sementara, dan ukur ulang setelah riwayat bertambah.
BASELINE_WINRATE = {
    ("UP", 1): 36.5, ("DOWN", 1): 42.6,
    ("UP", 3): 40.9, ("DOWN", 3): 47.0,
    ("UP", 5): 42.1, ("DOWN", 5): 48.3,
    ("UP", 20): 47.7, ("DOWN", 20): 48.1,     # 1 bulan  (n=13.877, median gerak 8,3%)
    ("UP", 60): 41.7, ("DOWN", 60): 56.4,     # 3 bulan  (n=3.917,  median gerak 15,4%)
    ("UP", 120): 40.7, ("DOWN", 120): 58.5,   # 6 bulan  (n=1.558,  median gerak 24,3%)
    ("UP", 250): 47.1, ("DOWN", 250): 50.6,   # 1 tahun  (n=257, TIPIS — lihat peringatan)
}

# Horizon yang boleh dipakai prediksi, dalam SESI BURSA. Nama dipakai UI & gaya fokus.
HORIZONS = {1: "1 hari", 3: "3 hari", 5: "1 minggu", 20: "1 bulan",
            60: "3 bulan", 120: "6 bulan", 250: "1 tahun"}
MAX_HORIZON = max(HORIZONS)


def baseline_for(direction: str, horizon: int) -> float:
    """Base rate untuk arah+horizon, DI-INTERPOLASI antar entri tabel (h=1/3/5).

    Dulu dibulatkan ke entri TERDEKAT, dan h=2 berjarak sama ke 1 dan ke 3 → `min()` memilih
    yang pertama = tabel h=1, penyebut TERKECIL. `backtest.run_horizons` memilih horizon dgn
    `edge_oos` terbesar, jadi h=2 menang bukan karena sinyalnya lebih kuat tapi karena
    pembandingnya paling rendah (audit 2026-08-31: h=2 dapat +3,9 pp dgn tabel h=1 vs -0,5 pp
    dgn tabel h=3 — vonisnya berbalik tanda). Interpolasi linear menghapus tebing itu; di luar
    rentang tabel np.interp menjepit ke ujung (perilaku lama untuk h>=5)."""
    if direction not in ("UP", "DOWN"):
        return 50.0
    known = sorted({h for _, h in BASELINE_WINRATE})
    return round(float(np.interp(horizon or 3, known,
                                 [BASELINE_WINRATE[(direction, h)] for h in known])), 1)


# Ambang "menang" — SATU angka untuk seluruh repo. orchestrator.is_win memakainya juga; kalau
# tiap modul menyimpan salinan sendiri, base rate dan win-rate bisa diukur dgn mistar berbeda
# dan selisihnya jadi karangan.
WIN_BAND_PCT = 0.5


def measured_baseline(direction: str, rows: list[dict], *, min_n: int = 60) -> float | None:
    """Base rate arah `direction` DIUKUR pada baris yang sedang dinilai. None bila sampel tipis.

    Kenapa ini ada (audit 2026-09-08, DB live 8 Sep). `BASELINE_WINRATE` diukur atas return
    HORIZON-TETAP di universe likuid sepanjang seluruh riwayat. Lengan taruhan live adalah
    populasi lain: resolusi bracket (`_resolve_target_hits`), median hold 0,94 hari, dan hanya
    di tanggal yang dipilih engine. Membandingkan keduanya = mengukur perbedaan POPULASI lalu
    menyebutnya edge. Terukur pada 273 taruhan nyata (h>=2):

        lengan     tabel   win-rate  CI selisih         vonis
        UP h>=2    41,0    50,7%     [+0,8, +13,5]      LOLOS  -> gerbang uang membuka
        UP h>=2    50,2*   50,7%     [-8,4,  +4,3]      gagal
        DOWN h>=2  47,5    35,0%     [-34,2, -1,4]      LOLOS  -> lengan ditutup
        DOWN h>=2  41,0*   35,0%     [-27,7, +5,1]      gagal
        (*) diukur di baris yang sama dgn fungsi ini

    Jadi DUA vonis produksi ("UP terbukti unggul", "DOWN terbukti rugi") sama-sama artefak
    pembanding, dan mesin mulai membeli 7-8 Sep atas bukti yang tak ada. Catatan di atas tabel
    sudah menyebut sendiri bahwa base rate bergeser sampai 12 pp antar rezim (UP h=3: 40,9
    seluruh riwayat vs 49,8 sejak 1 Jul 2026); untuk vonis LIVE, rezim yang cocok justru yang
    dibutuhkan. Tabel tetap dipakai sbg cadangan saat sampel < min_n.

    `rows` = baris resolved apa pun yang membawa `actual_pct` (SEMUA arah — kalau disaring ke
    arah yang sedang dinilai, penyebutnya ikut mewarisi seleksi engine dan bukan base rate lagi).
    """
    moves = [r["actual_pct"] for r in rows if r.get("actual_pct") is not None]
    if len(moves) < min_n or direction not in ("UP", "DOWN"):
        return None
    hit = (sum(1 for m in moves if m > WIN_BAND_PCT) if direction == "UP"
           else sum(1 for m in moves if m < -WIN_BAND_PCT))
    return round(100.0 * hit / len(moves), 1)


# Ambang aksi = base rate pasar + edge minimum, BUKAN angka mutlak. Lantai lama (BUY 60,
# gate DOWN 66) dipilih saat `probability` masih keluaran FORMULA mentah `50 + |score|*9`;
# di skala itu 60 berarti "|score| >= 1,11", sedikit di atas ambang arah 1,0. Setelah
# skill.calibrated_probability membuat angka itu berarti PELUANG MENANG SEBENARNYA, 60 jadi
# yardstick yang salah: base rate UP h=3 cuma 40,9%, jadi menuntut 60 = menuntut edge +19 pp
# dan program beli mati total (terukur 24-31 Agt 2026: nol BUY, portofolio 100% kas).
# Membandingkan keyakinan terkalibrasi ke base rate = membandingkan satuan yang sama.
def edge_floor(direction: str, horizon: int, edge_pp: float) -> float:
    """Ambang keyakinan untuk aksi: base rate pasar arah+horizon ini + `edge_pp`."""
    return round(baseline_for(direction, horizon) + edge_pp, 1)


def sample(horizon: int = 5, max_tickers: int = 150, history: int = 400) -> pd.DataFrame:
    """Kumpulkan (tanggal, fitur, return ke depan, turnover) — bahan mentah evaluasi."""
    recs = []
    for tk in repo.quote_tickers()[:max_tickers]:
        rows = repo.price_history(tk, history)
        if len(rows) < 80:
            continue
        df = pd.DataFrame(rows).rename(columns={"open": "Open", "high": "High", "low": "Low",
                                                "close": "Close", "volume": "Volume"})
        closes, vols, ts = df["Close"].values, df["Volume"].values, df["ts"].values
        for i in range(30, len(df) - horizon):
            feats = compute_features(df.iloc[: i + 1])
            if not feats or closes[i] <= 0:
                continue
            recs.append({"ticker": tk, "ts": str(ts[i])[:10], "feats": feats,
                         "fwd": (float(closes[i + horizon]) / float(closes[i]) - 1) * 100,
                         "turnover": float(closes[i]) * float(vols[i] or 0)})
    return pd.DataFrame(recs)


def block_bootstrap(x: np.ndarray, block: int = 5, iters: int = 3000,
                    seed: int = 0) -> tuple[float, float]:
    """CI 95% mean via bootstrap BLOK sirkuler — SATU implementasi untuk seluruh repo
    (`app/xsec.py` mengimpor yang ini; jangan tulis versi kedua).

    Unit resample = BLOK tanggal berurutan, bukan tanggal tunggal. Ini penting dan pernah
    salah di modul ini: versi pertama me-resample tanggal secara iid sambil mengaku "blok".
    Resample iid menutup korelasi lintas-saham DALAM satu tanggal, tapi TIDAK menutup
    korelasi serial ANTAR tanggal yang timbul karena label multi-hari beririsan (prediksi
    h=5 di hari t dan t+1 berbagi 4 hari yang sama). Akibatnya CI terlalu sempit dan
    gerbang bukti bisa PASS dari derau — persis penyakit yang modul ini dibuat untuk
    membunuh. `block` harus >= horizon label.
    """
    n = len(x)
    if n < block * 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, n, (iters, nb))
    take = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    means = x[take.reshape(iters, -1)[:, :n]].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def evaluate(df: pd.DataFrame, score_fn, *, top_pct: float = 0.10,
             liquid_only: bool = True, min_per_day: int = 2) -> dict:
    """Ukur sinyal `score_fn(feats) -> float` (makin tinggi = makin bullish).

    Bandingkan return saham ber-skor teratas vs SELURUH pasar di TANGGAL YANG SAMA
    (kontrol rezim: hari pasar jatuh tak dihitung sebagai kesalahan sinyal), lalu
    agregasi per-tanggal dengan bobot sama & CI bootstrap blok.
    """
    if df.empty or "turnover" not in df:
        return {"status": "insufficient", "n": 0,
                "note": "sampel kosong (riwayat harga belum cukup) — tak ada yang bisa diukur"}
    d = df[df["turnover"] >= MIN_TURNOVER].copy() if liquid_only else df.copy()
    if len(d) < 200:
        return {"status": "insufficient", "n": len(d),
                "note": "sampel likuid < 200 — jangan simpulkan apa pun"}
    d["score"] = [score_fn(f) for f in d["feats"]]
    d = d[d["score"].notna()]
    thr = d["score"].quantile(1 - top_pct)

    per_day = []
    for day, g in d.groupby("ts"):
        top = g[g["score"] >= thr]
        if len(top) >= min_per_day:
            per_day.append({"ts": day, "n_top": len(top), "top": top["fwd"].mean(),
                            "market": g["fwd"].mean()})
    if len(per_day) < 5:
        return {"status": "insufficient", "days": len(per_day),
                "note": "tanggal dgn cukup sinyal < 5 — jangan simpulkan apa pun"}

    p = pd.DataFrame(per_day)
    edges = (p["top"] - p["market"]).values
    lo, hi = block_bootstrap(edges)
    gross = float(p["top"].mean())
    return {
        "status": "ok",
        "n": int(len(d)), "days": int(len(p)),
        "baseline_market": round(float(p["market"].mean()), 3),
        "signal_gross": round(gross, 3),
        "net_after_fee": round(gross - FEE_ROUNDTRIP, 3),
        "edge_vs_baseline": round(float(edges.mean()), 3),
        "edge_median": round(float(np.median(edges)), 3),
        "days_positive_pct": round(float((edges > 0).mean() * 100), 1),
        "ci95": [round(lo, 3), round(hi, 3)],
        "significant": bool(lo > 0),
        "verdict": ("LULUS: edge > 0 di CI blok per-tanggal" if lo > 0 else
                    "GAGAL: CI memuat 0 — tak ada edge terbukti, jangan dipakai"),
    }


def verdict_winrate(records: list[dict], baseline_pct: float, *, side: str = "two",
                    min_days: int = 20, min_n: int = 40) -> dict:
    """Vonis untuk gerbang bukti berbasis win-rate (dipakai check_updates.py).

    `records` = [{"ts": "YYYY-MM-DD", "win": bool}]. Menguji apakah win-rate BEDA dari
    `baseline_pct` secara statistik, dengan tanggal sebagai unit (bukan baris) — satu hari
    buruk yang menyeret 20 prediksi sekaligus TIDAK boleh dihitung 20 bukti independen.

    `side` menentukan arah hipotesis, dan ini bukan detail kosmetik:
      - "below" → PASS hanya kalau win-rate terbukti DI BAWAH baseline (mis. item-2:
        buktikan arah UP memang lemah). Dua-sisi di sini keliru: win-rate yang justru
        LEBIH BAIK dari baseline akan ikut meloloskan gerbang "kalibrasi bermasalah",
        padahal itu bukti sebaliknya.
      - "above" → PASS kalau terbukti DI ATAS baseline (mis. klaim sebuah sinyal unggul).
      - "two"   → miskalibrasi arah mana pun.

    PASS hanya kalau CI 95% blok berada di sisi yang diminta. Ini yang membedakannya dari
    bar lama ("n>=40 & winrate<40%") yang bisa lolos dari fluktuasi 2-3 hari.
    """
    if len(records) < min_n:
        return {"pass": False, "n": len(records),
                "detail": f"n={len(records)} < {min_n} — sampel belum cukup"}
    df = pd.DataFrame(records)
    per_day = df.groupby("ts")["win"].mean() * 100
    if len(per_day) < min_days:
        return {"pass": False, "n": len(records), "days": int(len(per_day)),
                "detail": f"{len(per_day)} tanggal < {min_days} — hari efektif belum cukup "
                          f"(n={len(records)} baris menyesatkan: prediksi sehari berkorelasi)"}
    dev = per_day.values - baseline_pct
    lo, hi = block_bootstrap(dev)
    wr = float(df["win"].mean() * 100)
    ok = {"below": hi < 0, "above": lo > 0}.get(side, lo > 0 or hi < 0)
    label = {"below": "terbukti DI BAWAH baseline", "above": "terbukti DI ATAS baseline"}.get(
        side, "BEDA nyata dari baseline")
    return {"pass": bool(ok), "n": len(records), "days": int(len(per_day)),
            "winrate": round(wr, 1), "baseline": round(baseline_pct, 1), "side": side,
            "ci95_vs_baseline": [round(lo, 1), round(hi, 1)],
            "detail": f"win-rate {wr:.1f}% vs baseline {baseline_pct:.1f}% | "
                      f"{len(per_day)} tanggal | CI selisih [{lo:+.1f}, {hi:+.1f}] pp | "
                      f"{label if ok else 'belum terbukti (CI belum di sisi yang diuji)'}"}


def verdict_paired(records: list[dict], *, min_days: int = 20, min_n: int = 40) -> dict:
    """Vonis A/B berpasangan per-tanggal (mis. item-3: sinyal DENGAN vs TANPA konfirmasi asing).

    `records` = [{"ts", "win": bool, "group": "a"|"b"}]. Di TIAP tanggal dihitung win-rate
    kelompok A dan kelompok B, lalu selisihnya — jadi rezim pasar hari itu membatalkan diri
    (hari merah menekan kedua kelompok sama-sama). Hanya tanggal yang punya KEDUA kelompok
    yang dipakai; itulah arti "berpasangan".

    Bar lama "oos_with > oos_without + 1pt" membandingkan dua persentase tanpa CI dan tanpa
    pasangan tanggal — selisih 1pt pada n=40 sepenuhnya di dalam derau.
    """
    if len(records) < min_n:
        return {"pass": False, "n": len(records),
                "detail": f"n={len(records)} < {min_n} — sampel belum cukup"}
    df = pd.DataFrame(records)
    g = df.pivot_table(index="ts", columns="group", values="win", aggfunc="mean") * 100
    if "a" not in g or "b" not in g:
        return {"pass": False, "n": len(records),
                "detail": "salah satu kelompok kosong — tak ada yang bisa dibandingkan"}
    g = g.dropna(subset=["a", "b"])          # hanya tanggal ber-PASANGAN
    if len(g) < min_days:
        return {"pass": False, "n": len(records), "days": int(len(g)),
                "detail": f"{len(g)} tanggal ber-pasangan < {min_days} — belum cukup"}
    diff = (g["a"] - g["b"]).values
    lo, hi = block_bootstrap(diff)
    return {"pass": bool(lo > 0), "n": len(records), "days": int(len(g)),
            "mean_diff": round(float(diff.mean()), 1), "ci95": [round(lo, 1), round(hi, 1)],
            "detail": f"selisih A-B {diff.mean():+.1f} pp | {len(g)} tanggal berpasangan | "
                      f"CI [{lo:+.1f}, {hi:+.1f}] pp | "
                      f"{'A lebih baik' if lo > 0 else 'belum terbukti lebih baik'}"}


def _print(name: str, r: dict) -> None:
    print(f"\n=== {name} ===")
    if r.get("status") != "ok":
        print(f"  {r.get('note')}  ({r})")
        return
    print(f"  sampel {r['n']} baris / {r['days']} tanggal")
    print(f"  baseline pasar (tanggal sama) : {r['baseline_market']:+.3f}%")
    print(f"  return sinyal (kotor)         : {r['signal_gross']:+.3f}%")
    print(f"  setelah fee {FEE_ROUNDTRIP}%           : {r['net_after_fee']:+.3f}%")
    print(f"  edge vs baseline              : {r['edge_vs_baseline']:+.3f}% "
          f"(median {r['edge_median']:+.3f}%, positif {r['days_positive_pct']}% tanggal)")
    print(f"  CI 95% blok per-tanggal       : [{r['ci95'][0]:+.3f}%, {r['ci95'][1]:+.3f}%]")
    print(f"  -> {r['verdict']}")


def _self_check() -> None:
    """Sinyal ORACLE (tahu masa depan) harus LULUS; sinyal acak harus GAGAL.
    Kalau harness sendiri tak bisa membedakan keduanya, semua angkanya tak berarti."""
    rng = np.random.default_rng(7)
    rows = []
    for day in range(60):
        for tk in range(30):
            fwd = float(rng.normal(0, 3))
            rows.append({"ticker": f"T{tk}", "ts": f"2026-01-{day % 28 + 1:02d}",
                         "feats": {"fwd": fwd, "noise": float(rng.normal())},
                         "fwd": fwd, "turnover": 5e9})
    df = pd.DataFrame(rows)
    good = evaluate(df, lambda f: f["fwd"], min_per_day=2)
    bad = evaluate(df, lambda f: f["noise"], min_per_day=2)
    assert good["significant"], f"oracle harus terdeteksi punya edge: {good}"
    assert not bad["significant"], f"sinyal acak tak boleh lolos: {bad}"

    # verdict_winrate: koin adil vs baseline 50 = TIDAK beda; koin 25% = beda nyata.
    fair = [{"ts": f"d{i//5:02d}", "win": bool(rng.random() < 0.5)} for i in range(300)]
    biased = [{"ts": f"d{i//5:02d}", "win": bool(rng.random() < 0.25)} for i in range(300)]
    assert not verdict_winrate(fair, 50.0)["pass"], "koin adil tak boleh lolos gerbang"
    assert verdict_winrate(biased, 50.0)["pass"], "penyimpangan 25pp harus terdeteksi"
    # sampel besar tapi HARI sedikit harus ditolak (inti perbaikan gerbang)
    few_days = [{"ts": f"d{i % 3}", "win": bool(rng.random() < 0.25)} for i in range(300)]
    assert not verdict_winrate(few_days, 50.0)["pass"], "3 tanggal tak boleh lolos walau n=300"

    # verdict_paired: A identik B = tidak lolos; A jelas lebih baik = lolos.
    same, better = [], []
    for i in range(600):
        day, grp = f"d{i // 10:02d}", ("a" if i % 2 else "b")
        same.append({"ts": day, "group": grp, "win": bool(rng.random() < 0.5)})
        p = 0.75 if grp == "a" else 0.35
        better.append({"ts": day, "group": grp, "win": bool(rng.random() < p)})
    assert not verdict_paired(same)["pass"], "A==B tak boleh lolos"
    assert verdict_paired(better)["pass"], "A jauh lebih baik harus lolos"

    # `side`: win-rate JAUH LEBIH BAIK dari baseline tak boleh meloloskan gerbang
    # "terbukti lemah" (side='below') — pernah salah, lihat docstring verdict_winrate.
    strong = [{"ts": f"d{i//5:02d}", "win": bool(rng.random() < 0.80)} for i in range(300)]
    assert not verdict_winrate(strong, 50.0, side="below")["pass"], \
        "win-rate tinggi tak boleh lolos gerbang 'terbukti DI BAWAH baseline'"
    assert verdict_winrate(strong, 50.0, side="above")["pass"], "sisi 'above' harus lolos"
    assert verdict_winrate(biased, 50.0, side="below")["pass"], "sisi 'below' harus lolos"
    print("self-check OK: oracle LULUS, acak GAGAL, gerbang win-rate & A/B benar, sisi uji benar")


def refresh_baseline(horizons=(1, 3, 5), max_tickers: int = 150) -> dict:
    """Hitung ulang BASELINE_WINRATE dari data harga sekarang (konstanta di atas bisa basi
    kalau universe/rezim berubah). Cetak blok siap tempel — sengaja TIDAK menulis otomatis
    supaya perubahan angka acuan selalu lewat mata manusia."""
    out = {}
    for h in horizons:
        df = sample(horizon=h, max_tickers=max_tickers)
        if df.empty:
            continue
        f = df[df["turnover"] >= MIN_TURNOVER]["fwd"]
        out[("UP", h)] = round(float((f > 0.5).mean() * 100), 1)
        out[("DOWN", h)] = round(float((f < -0.5).mean() * 100), 1)
        print(f"  h={h}: n={len(f)} likuid | UP {out[('UP', h)]}% | DOWN {out[('DOWN', h)]}%")
    print("\nBASELINE_WINRATE = {")
    for h in horizons:
        if ("UP", h) in out:
            print(f'    ("UP", {h}): {out[("UP", h)]}, ("DOWN", {h}): {out[("DOWN", h)]},')
    print("}")
    return out


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if "--self-check" in sys.argv:
        _self_check()
        raise SystemExit(0)

    from app import db, model
    from app.agents.heuristic import score_signals
    db.init_db()

    if "--baseline" in sys.argv:
        print("menghitung ulang base rate pasar…")
        refresh_baseline()
        raise SystemExit(0)

    print("mengumpulkan sampel historis…")
    df = sample()
    print(f"{len(df)} sampel, {df['ts'].nunique()} tanggal, "
          f"{(df['turnover'] >= MIN_TURNOVER).mean() * 100:.0f}% likuid")

    _print("score_signals (heuristik, dipakai LIVE)",
           evaluate(df, lambda f: score_signals(f, news=0)[0]))
    _print("model logreg p_up", evaluate(df, model.predict_proba))
    print("\nCatatan: model.json dilatih atas rentang yang beririsan dgn sampel ini "
          "(sebagian in-sample). Angka OOS jujur ada di scratchpad walkfwd/robust.")
