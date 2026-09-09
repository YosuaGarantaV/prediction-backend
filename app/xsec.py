"""Model prediksi CROSS-SECTIONAL IDX (h=5) — desain Fable 5, audit 2026-07-20.

Beda mendasar dari `app/model.py` (logreg lama) dan `heuristic.score_signals`:
model ini TIDAK memprediksi "saham X naik atau turun". Ia memprediksi URUTAN saham
dalam SATU tanggal. Alasannya terukur: model lama menghabiskan kapasitas belajar rezim
pasar (naik/turun serentak), dan metrik per-BARIS-nya menipu — AUC pooled 0,591 tapi
spread desil per-TANGGAL -0,33%.

Prinsip yang mengikat seluruh modul ini:
  - N efektif = jumlah TANGGAL (~214/5 ≈ 40 setelah overlap label 5 hari), BUKAN 31.403 baris.
  - Semua metrik per-tanggal. Metrik pooled per-baris DILARANG muncul di laporan.
  - Budget percobaan M <= 5 (registri di RESULT_FILE). Bonferroni dipakai di p-value.
  - Model utama M0 punya NOL parameter hasil fitting → tak bisa overfit. M1 (Fama-MacBeth)
    hanya menggantikannya kalau menang >= +0,01 IC out-of-fold.

DEVIASI dari spec (dicatat sesuai Bagian 9 spec):
  fitur `news_decay` & `has_news` DIBUANG dari model. Sebab: 3.142 dari 3.908 baris berita
  bertanggal Juli 2026 (80%), sedangkan riwayat harga membentang Jul 2025-Jul 2026 — fitur
  berita akan ~0 di seluruh periode training lalu tiba-tiba aktif di produksi, yaitu persis
  ketidakcocokan live-vs-backtest yang tak teruji. Berita tetap tampil sebagai overlay
  (ditandai "tidak masuk model"). 9 fitur → 7.

CLI:  python -m app.xsec              # protokol validasi penuh
      python -m app.xsec --self-check # uji harness dgn sinyal sintetis
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import config
from app import repo
from app.eval import block_bootstrap as _eval_block_bootstrap

HORIZON = 5
RESULT_FILE = config.DATA_DIR / "xsec_result.json"
SEED = 20260720
# ponytail: tak ada MODEL_FILE / jalur serving. Model ini GAGAL gerbangnya sendiri
# (F7 tilt beta) jadi tak ada yang perlu dipersist; menulis artefak model yang tak
# dipakai hanya mengundang orang menyambungkannya ke live tanpa lolos validasi.

# Fitur + TANDA PRIOR (dari teori, bukan dari data ini). log_turnover = kontrol, tanpa prior.
FEATURES = ["mom_comp", "rev_5d", "prox_52w_high", "volatility_20d",
            "vol_vs_avg", "amihud_20d", "log_turnover"]
PRIOR_SIGN = {"mom_comp": +1, "rev_5d": -1, "prox_52w_high": +1, "volatility_20d": -1,
              "vol_vs_avg": +1, "amihud_20d": +1, "log_turnover": 0}

MIN_NAMES_PER_DAY = 30      # tanggal dgn nama tradeable < ini: skip (training) / no-view (live)
THIN_DAY = 50               # < ini ditandai "tipis" utk uji F6
FEE_PCT = (config.FEE_BUY + config.FEE_SELL) * 100   # round-trip 0.40% dari config yang sama
                                                     # dipakai paper trading (satu sumber angka)


# ---------------------------------------------------------------- fitur per-ticker
def ticker_features(df: pd.DataFrame) -> pd.DataFrame:
    """Fitur harian 1 ticker, vektor (bukan loop per-baris).

    SATU definisi dipakai training MAUPUN produksi — kalau keduanya beda, semua validasi
    jadi omong kosong. `df` = kolom ts/close/volume urut naik.
    """
    c, v = df["close"].astype(float), df["volume"].astype(float).fillna(0)
    out = pd.DataFrame({"ts": df["ts"].astype(str).str[:10], "close": c})
    ret1 = c.pct_change()

    # --- momentum menengah (4 varian kolinear digabung jadi 1 dof) ---
    d = c.diff()
    gain = d.clip(lower=0).rolling(14).mean()
    loss = (-d.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_hist = (macd - macd.ewm(span=9, adjust=False).mean()) / c * 100
    sma20, sd20 = c.rolling(20).mean(), c.rolling(20).std()
    pctb = (c - (sma20 - 2 * sd20)) / (4 * sd20).replace(0, np.nan)
    mom10 = c / c.shift(10) - 1
    out["_rsi"], out["_macd"], out["_pctb"], out["_mom10"] = rsi, macd_hist, pctb, mom10

    # --- sisanya ---
    out["rev_5d"] = c / c.shift(HORIZON) - 1
    out["prox_52w_high"] = c / c.rolling(252, min_periods=60).max() - 1
    out["volatility_20d"] = ret1.rolling(20).std() * 100
    out["vol_vs_avg"] = v / v.rolling(20).mean().replace(0, np.nan)

    turnover = c * v
    out["med_turnover_20"] = turnover.rolling(20).median()
    # Amihud: dampak harga per rupiah diperdagangkan = proksi premi illikuiditas.
    out["amihud_20d"] = np.log1p((ret1.abs() / turnover.replace(0, np.nan)).rolling(20).mean() * 1e12)
    out["log_turnover"] = np.log1p(out["med_turnover_20"])
    out["zero_vol_20"] = (v == 0).rolling(20).sum()
    # ARA IDX bervariasi per pita harga; ~+20% dipakai sbg pendekatan (hanya utk EKSKLUSI).
    out["ara_days_5"] = (ret1 >= 0.199).rolling(5).sum()
    out["fwd"] = (c.shift(-HORIZON) / c - 1) * 100
    # F4 (spec 1.3): sinyal terbit SETELAH close t, jadi eksekusi paling awal = open t+1.
    # `fwd_open` = masuk open[t+1], keluar open[t+1+HORIZON]. Kalau spread runtuh >50% di sini,
    # sinyalnya artefak close-to-close (khas bid-ask bounce di saham tipis), bukan alpha.
    o = df["open"].astype(float) if "open" in df else c
    out["fwd_open"] = (o.shift(-(HORIZON + 1)) / o.shift(-1) - 1) * 100
    return out


def build_panel(max_tickers: int = 400, history: int = 400) -> pd.DataFrame:
    """Panel (ts, ticker, fitur, fwd) untuk SELURUH universe."""
    frames = []
    for tk in repo.quote_tickers()[:max_tickers]:
        rows = repo.price_history(tk, history)
        if len(rows) < 80:
            continue
        f = ticker_features(pd.DataFrame(rows))
        f["ticker"] = tk
        frames.append(f)
    if not frames:
        return pd.DataFrame()
    p = pd.concat(frames, ignore_index=True)
    # mom_comp = rata-rata RANK 4 varian momentum (rank dulu per-tanggal, lalu dirata-rata)
    for col in ["_rsi", "_macd", "_pctb", "_mom10"]:
        p[col + "_r"] = p.groupby("ts")[col].rank(pct=True) - 0.5
    p["mom_comp"] = p[["_rsi_r", "_macd_r", "_pctb_r", "_mom10_r"]].mean(axis=1)
    return p


def tradeable(p: pd.DataFrame, *, training: bool = True) -> pd.DataFrame:
    """Filter universe (spec 1.1) — dikerjakan SEBELUM modeling, bukan lewat label."""
    m = (
        (p["med_turnover_20"] >= config.MIN_TURNOVER)
        & (p["close"] >= 100)
        & (p["zero_vol_20"] <= 6)
        & (p["ara_days_5"].fillna(0) == 0)
        & p["mom_comp"].notna() & p["rev_5d"].notna() & p["volatility_20d"].notna()
        & (p["volatility_20d"] > 0)
    )
    if training:
        m &= p["fwd"].notna()
    return p[m].copy()


# ---------------------------------------------------------------- rank & target
def _crank(s: pd.Series) -> pd.Series:
    """Centered rank [-0.5, +0.5], ties dirata-rata. Imun outlier — tanpa winsorization."""
    n = s.notna().sum()
    if n <= 1:
        return pd.Series(0.0, index=s.index)
    return (s.rank(method="average", pct=True) - 0.5).fillna(0.0)


def prepare(p: pd.DataFrame, *, training: bool = True) -> pd.DataFrame:
    """Rank fitur per-tanggal + (kalau training) bangun target rank."""
    d = tradeable(p, training=training)
    counts = d.groupby("ts")["ticker"].transform("size")
    d = d[counts >= MIN_NAMES_PER_DAY].copy()
    if d.empty:
        return d
    for f in FEATURES:
        d[f + "_r"] = d.groupby("ts")[f].transform(_crank)
    if training:
        # target: excess vs MEDIAN tanggal (bukan mean — mean tercemar right-skew),
        # diskala volatilitas, di-clip, lalu di-rank → tiap tanggal berbobot sama.
        med = d.groupby("ts")["fwd"].transform("median")
        d["_z"] = ((d["fwd"] - med) / (d["volatility_20d"] * np.sqrt(HORIZON))).clip(-3, 3)
        d["y"] = d.groupby("ts")["_z"].transform(_crank)
    return d


# ---------------------------------------------------------------- model
def score_m0(d: pd.DataFrame) -> np.ndarray:
    """M0: composite equal-weight bertanda prior. NOL parameter di-fit → tak bisa overfit."""
    signed = [PRIOR_SIGN[f] * d[f + "_r"].values for f in FEATURES if PRIOR_SIGN[f] != 0]
    return np.sum(signed, axis=0) / len(signed)


def fit_m1(d: pd.DataFrame) -> dict:
    """M1: Fama-MacBeth — OLS per tanggal, lalu rata-rata bobot antar tanggal.

    SE memakai pembagi sqrt(T/5), bukan sqrt(T): label 5 hari overlap → b_t berkorelasi
    serial, jadi tanggal bukan unit independen penuh. Koreksi kasar tapi konservatif
    (pengganti Newey-West tanpa scipy). Gerbang: |t| >= 2 DAN tanda == prior.
    """
    cols = [f + "_r" for f in FEATURES]
    bs = []
    for _, g in d.groupby("ts"):
        if len(g) < len(cols) + 5:
            continue
        X = np.c_[np.ones(len(g)), g[cols].values]
        b, *_ = np.linalg.lstsq(X, g["y"].values, rcond=None)
        bs.append(b[1:])
    if len(bs) < 20:
        return {"w": {f: 0.0 for f in FEATURES}, "T": len(bs), "note": "tanggal < 20"}
    B = np.array(bs)
    w, T = B.mean(0), len(B)
    se = B.std(0, ddof=1) / np.sqrt(max(T / HORIZON, 1.0))
    tstat = np.divide(w, se, out=np.zeros_like(w), where=se > 0)
    final = {}
    for j, f in enumerate(FEATURES):
        prior = PRIOR_SIGN[f]
        ok = abs(tstat[j]) >= 2.0 and (prior == 0 or np.sign(w[j]) == prior)
        # James-Stein: bobot yang lolos pun dikerut sesuai kekuatan t-nya
        final[f] = float(w[j] * max(0.0, 1 - 1 / tstat[j] ** 2)) if ok else 0.0
    return {"w": final, "T": T, "t": {f: round(float(tstat[j]), 2) for j, f in enumerate(FEATURES)}}


def score_m1(d: pd.DataFrame, w: dict) -> np.ndarray:
    return np.sum([w.get(f, 0.0) * d[f + "_r"].values for f in FEATURES], axis=0)


# ---------------------------------------------------------------- metrik
def ic_per_date(d: pd.DataFrame, scores: np.ndarray) -> pd.Series:
    """IC Spearman per tanggal = korelasi Pearson atas RANK (numpy murni, tanpa scipy)."""
    t = d.assign(_s=scores)
    out = {}
    for ts, g in t.groupby("ts"):
        if len(g) < 10:
            continue
        a = pd.Series(g["_s"].values).rank().values
        b = pd.Series(g["y"].values).rank().values
        if a.std() == 0 or b.std() == 0:
            continue
        out[ts] = float(np.corrcoef(a, b)[0, 1])
    return pd.Series(out).sort_index()


def block_bootstrap(x: np.ndarray, block: int = 10, iters: int = 2000,
                    seed: int = SEED) -> tuple[float, float]:
    """CI 95% mean dgn blok tanggal berurutan (circular) — blok 10 = 2x horizon, menutup
    overlap label 5 hari dan sebagian autokorelasi IC.

    Implementasinya SATU di `app.eval.block_bootstrap`; di sini cuma default yang beda
    (blok 10 & seed modul ini). Jangan tulis versi ketiga.
    """
    return _eval_block_bootstrap(x, block=block, iters=iters, seed=seed)


def permutation_p(d: pd.DataFrame, scores: np.ndarray, observed: float,
                  iters: int = 1000, seed: int = SEED) -> float:
    """Null: acak URUTAN score ANTAR NAMA di dalam tiap tanggal (label tetap).

    Mempertahankan struktur tanggal, korelasi cross-sectional, gerak pasar, dan distribusi
    score — yang diputus hanya kaitan saham-ke-saham, persis hipotesis yang diuji.
    """
    rng = np.random.default_rng(seed)
    t = d.assign(_s=scores)
    groups = [(g["_s"].values, pd.Series(g["y"].values).rank().values)
              for _, g in t.groupby("ts") if len(g) >= 10]
    ge = 0
    for _ in range(iters):
        ics = []
        for s, yr in groups:
            sp = pd.Series(rng.permutation(s)).rank().values
            if sp.std() == 0 or yr.std() == 0:
                continue
            ics.append(np.corrcoef(sp, yr)[0, 1])
        if ics and np.mean(ics) >= observed:
            ge += 1
    return (ge + 1) / (iters + 1)


# ---------------------------------------------------------------- walk-forward
def walk_forward(d: pd.DataFrame, *, min_train: int = 120, test_block: int = 21,
                 purge: int = HORIZON, embargo: int = HORIZON) -> dict:
    """Expanding window, purge + embargo. Mengembalikan IC per-tanggal test utk M0 & M1.

    Mengembalikan juga SKOR OUT-OF-FOLD per baris test. Ini bukan kenyamanan: kalau
    spread/permutasi/F4/F7 memakai skor yang di-fit atas seluruh tanggal non-test, M1 akan
    melihat masa depan fold-fold awal (dan tanggal purge-gap) → cek ekonomi yang masuk
    `verdict_pass` terkontaminasi walau deret IC-nya bersih. M0 kebal (nol parameter),
    tapi jalurnya harus benar untuk kedua model.
    """
    dates = sorted(d["ts"].unique())
    ic0, ic1, folds, oof = {}, {}, [], []
    i = min_train
    while i + test_block <= len(dates):
        test_days = dates[i:i + test_block]
        # purge: buang training yang jendela label [t, t+5] menyentuh blok test;
        # embargo: tambah jeda setelah blok test sebelum tanggal boleh masuk fold berikutnya.
        train_days = dates[:max(0, i - purge)]
        tr = d[d["ts"].isin(train_days)]
        te = d[d["ts"].isin(test_days)]
        if len(tr) < 500 or te.empty:
            i += test_block + embargo
            continue
        sc0 = score_m0(te)
        m1 = fit_m1(tr)
        sc1 = score_m1(te, m1["w"])
        s0, s1 = ic_per_date(te, sc0), ic_per_date(te, sc1)
        ic0.update(s0.to_dict())
        ic1.update(s1.to_dict())
        oof.append(te.assign(_s_m0=sc0, _s_m1=sc1))
        folds.append({"test_from": test_days[0], "test_to": test_days[-1],
                      "n_train_days": len(train_days), "n_test_days": len(test_days),
                      "ic_m0": round(float(s0.mean()), 4), "ic_m1": round(float(s1.mean()), 4),
                      "m1_weights": {k: round(v, 4) for k, v in m1["w"].items()}})
        i += test_block + embargo
    return {"ic_m0": pd.Series(ic0).sort_index(), "ic_m1": pd.Series(ic1).sort_index(),
            "folds": folds,
            "oof": pd.concat(oof, ignore_index=True) if oof else pd.DataFrame()}


# ---------------------------------------------------------------- ekonomi
def spread_per_date(d: pd.DataFrame, scores: np.ndarray, *, top_q: float = 0.2,
                    fwd_col: str = "fwd") -> pd.Series:
    """Spread ekonomi per tanggal: return kuintil-atas MINUS median universe tanggal itu.

    Median (bukan mean) supaya satu saham meroket tak menciptakan 'edge' — pelajaran
    langsung dari 87 sampel ekor yang dulu menipu spread desil.
    """
    t = d.assign(_s=scores)
    out = {}
    for ts, g in t.groupby("ts"):
        if len(g) < 10:
            continue
        thr = g["_s"].quantile(1 - top_q)
        top = g[g["_s"] >= thr]
        if len(top) < 2:
            continue
        out[ts] = float(top[fwd_col].median() - g[fwd_col].median())
    return pd.Series(out).sort_index()


def regime_split(d: pd.DataFrame, scores: np.ndarray, ic: pd.Series) -> dict:
    """GERBANG ANTI-BETA — apakah "edge" ini sebenarnya taruhan arah pasar terselubung?

    Ditemukan 2026-07-20 dan inilah alasan gerbang ini ada permanen: composite M0 mencetak
    IC 0,097 yang tampak kuat, tapi korelasi(return pasar, IC harian) = -0,82. Dipisah:
    IC +0,283 saat pasar turun (92% tanggal positif) vs -0,177 saat pasar naik (18%).
    Penyebabnya low-vol + dekat-52w-high = saham ber-BETA RENDAH; saat pasar jatuh mereka
    otomatis mengalahkan median. De-mean cross-sectional membuang LEVEL pasar, TAPI TIDAK
    membuang dispersi beta — jadi tilt defensif lolos menyamar sebagai alpha.

    Periode test kebetulan rata-rata -1,75%, sehingga backtest terlihat bagus. Di siklus
    penuh ekspektasinya ~0, dan di pasar naik ia RUGI.

    Uji permutasi TIDAK bisa menangkap ini (mengacak dalam tanggal tak mengubah eksposur
    beta); hanya pemisahan rezim + bootstrap blok yang bisa. Karena itu wajib.
    """
    mkt = d.groupby("ts")["fwd"].median()
    common = ic.index.intersection(mkt.index)
    ic, mkt = ic[common], mkt[common]
    if len(common) < 20 or (mkt < 0).sum() < 5 or (mkt >= 0).sum() < 5:
        return {"lolos": None, "note": "tanggal per rezim < 5 — tak bisa diuji"}
    dn, up = ic[mkt < 0], ic[mkt >= 0]
    corr = float(np.corrcoef(mkt.values, ic.values)[0, 1])
    # Lolos: IC di pasar NAIK tidak negatif secara berarti, DAN korelasi tak ekstrem.
    ok = bool(up.mean() > -0.02 and corr > -0.5)
    return {"ic_pasar_turun": round(float(dn.mean()), 4), "n_turun": int(len(dn)),
            "ic_pasar_naik": round(float(up.mean()), 4), "n_naik": int(len(up)),
            "korelasi_ic_vs_pasar": round(corr, 3), "lolos": ok,
            "tafsir": ("sinyal konsisten lintas rezim" if ok else
                       "TILT BETA menyamar jadi alpha — menang hanya saat pasar turun, "
                       "rugi saat pasar naik; ekspektasi siklus penuh ~0")}


def _decide(ic: pd.Series, spread: pd.Series, pval: float, m_budget: int,
            thin_ic: float | None) -> dict:
    """Aturan keputusan spec 5.5 + falsifikasi Bagian 7. Semua syarat harus terpenuhi."""
    ic_mean = float(ic.mean())
    lo, hi = block_bootstrap(ic.values)
    slo, shi = block_bootstrap(spread.values)
    net = float(spread.mean()) - FEE_PCT
    alpha = 0.01 / max(m_budget, 1)
    checks = {
        "IC CI 95% > 0": bool(lo > 0),
        "mean IC >= 0.03": bool(ic_mean >= 0.03),
        f"permutation p < {alpha:.4f} (Bonferroni M={m_budget})": bool(pval < alpha),
        "batas bawah CI net spread > -0.1%": bool(slo - FEE_PCT > -0.1),
    }
    if thin_ic is not None:
        checks["IC bertahan tanpa tanggal tipis (F6)"] = bool(thin_ic >= 0.02)
    return {
        "ic_mean": round(ic_mean, 4), "ic_ci95": [round(lo, 4), round(hi, 4)],
        "ic_days": int(len(ic)),
        "spread_gross_mean": round(float(spread.mean()), 3),
        "spread_ci95": [round(slo, 3), round(shi, 3)],
        "spread_net_mean": round(net, 3),
        "permutation_p": round(pval, 5), "alpha_bonferroni": round(alpha, 5),
        "checks": checks, "verdict_pass": all(checks.values()),
    }


def run(*, m_budget: int = 2, permutations: int = 1000) -> dict:
    """Protokol validasi penuh (spec Bagian 5). Menulis RESULT_FILE."""
    panel = build_panel()
    d = prepare(panel, training=True)
    if d.empty:
        return {"status": "insufficient", "note": "panel kosong setelah filter universe"}

    wf = walk_forward(d)
    ic0, ic1 = wf["ic_m0"], wf["ic_m1"]
    if len(ic0) < 20:
        return {"status": "insufficient", "note": f"hanya {len(ic0)} tanggal test — butuh >= 20"}

    # M1 menggantikan M0 hanya kalau menang >= +0.01 IC (spec 4.1)
    use_m1 = bool(ic1.mean() >= ic0.mean() + 0.01)
    ic = ic1 if use_m1 else ic0
    # SKOR OUT-OF-FOLD (bukan re-fit atas seluruh non-test): tiap baris test diberi skor
    # oleh model yang HANYA melihat data sebelum blok test-nya sendiri. Tanpa ini, cek
    # ekonomi/F4/F7 untuk M1 memakai bobot yang sudah melihat masa depan fold awal.
    te = wf["oof"]
    scores = te["_s_m1"].values if use_m1 else te["_s_m0"].values

    spread = spread_per_date(te, scores)
    pval = permutation_p(te, scores, float(ic.mean()), iters=permutations)

    # F4: entry realistis open[t+1] — uji apakah sinyal selamat dari timing eksekusi.
    mask_o = te["fwd_open"].notna().values
    te_o = te[mask_o]
    spread_open = (spread_per_date(te_o, scores[mask_o], fwd_col="fwd_open")
                   if len(te_o) > 200 else pd.Series(dtype=float))
    if len(spread_open) >= 20:
        so_lo, so_hi = block_bootstrap(spread_open.values)
        drop = 1 - (spread_open.mean() / spread.mean()) if spread.mean() != 0 else 1.0
        f4 = {"spread_open_mean": round(float(spread_open.mean()), 3),
              "spread_open_ci95": [round(so_lo, 3), round(so_hi, 3)],
              "spread_open_net": round(float(spread_open.mean()) - FEE_PCT, 3),
              "penurunan_vs_close": round(float(drop) * 100, 1),
              "lolos": bool(drop <= 0.5)}
    else:
        f4 = {"lolos": None, "note": "data open tak cukup untuk uji F4"}

    # F6: apakah sinyal hanya hidup di tanggal ber-universe tipis?
    counts = te.groupby("ts")["ticker"].size()
    fat_days = counts[counts >= THIN_DAY].index
    thin_ic = float(ic[ic.index.isin(fat_days)].mean()) if len(fat_days) >= 10 else None

    regime = regime_split(te, scores, ic)
    verdict = _decide(ic, spread, pval, m_budget, thin_ic)
    verdict["checks"]["F4 entry open[t+1] tak runtuh >50%"] = (
        True if f4.get("lolos") is None else bool(f4["lolos"]))
    verdict["checks"]["F7 bukan tilt beta (konsisten lintas rezim)"] = (
        True if regime.get("lolos") is None else bool(regime["lolos"]))
    verdict["verdict_pass"] = all(verdict["checks"].values())
    verdict["f4_open_entry"] = f4
    verdict["f7_regime"] = regime
    res = {
        "status": "ok", "ts": datetime.now(timezone.utc).isoformat(),
        "horizon": HORIZON, "features": FEATURES, "seed": SEED,
        "n_rows": int(len(d)), "n_dates": int(d["ts"].nunique()),
        "n_test_dates": int(len(ic)),
        "model_used": "M1 (Fama-MacBeth)" if use_m1 else "M0 (composite equal-weight)",
        "ic_m0_mean": round(float(ic0.mean()), 4), "ic_m1_mean": round(float(ic1.mean()), 4),
        "m1_beats_m0": use_m1,
        "ic_thin_excluded": None if thin_ic is None else round(thin_ic, 4),
        # DIAGNOSTIK saja — untuk memahami dari mana IC datang. DILARANG dipakai memilih
        # fitur (memilih pemenang di sini = persis mekanisme overfit yang diaudit).
        "ic_per_feature_diagnostic": {
            f: round(float(ic_per_date(te, PRIOR_SIGN[f] * te[f + "_r"].values).mean()), 4)
            for f in FEATURES if PRIOR_SIGN[f] != 0},
        "folds": wf["folds"],
        "registry": {"M_budget": m_budget,
                     "T1": "M0 composite h=5 universe 1.1",
                     "T2": "M1 Fama-MacBeth h=5 universe 1.1",
                     "deviasi": "news_decay & has_news dibuang (80% berita bertanggal Jul 2026 "
                                "→ tak bisa di-backtest); 9 fitur → 7"},
        **verdict,
    }
    RESULT_FILE.write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    return res


# ---------------------------------------------------------------- jalur BAYANGAN (shadow)
_SHADOW: dict = {}
SHADOW_TTL_S = 1800.0     # panel harian; 30 mnt cukup, membangunnya mahal (~400 ticker)


def shadow_scores() -> dict[str, float]:
    """Skor cross-sectional M0 untuk universe tradeable HARI INI → persentil [0,1] per ticker.

    BAYANGAN, dan itu disengaja: nilai ini DICATAT di tiap prediksi (repo.save_prediction)
    tapi TIDAK menyetir arah, keyakinan, maupun aksi apa pun. Alasannya ada di kepala modul —
    M0 GAGAL gerbangnya sendiri pada 2026-07-20 (`verdict_pass: false`; F7 memvonis "TILT BETA
    menyamar jadi alpha": IC +0,283 saat pasar turun vs -0,177 saat pasar naik, korelasi IC
    dengan arah pasar -0,818). Menyambungkannya ke keputusan tanpa lolos validasi persis
    yang dilarang komentar di atas.

    Yang dilakukan di sini cuma MENGUMPULKAN BUKTI berdampingan: model lama (`model.predict_proba`)
    dan model cross-sectional ini dicatat pada baris prediksi yang sama, sehingga begitu
    outcome-nya matang keduanya bisa diadu di atas populasi yang identik — bukan di atas dua
    backtest berbeda dengan populasi berbeda. Keputusan pensiun/promosi diambil dari data itu,
    bukan sekarang.

    {} kalau data harga belum cukup. Di-cache SHADOW_TTL_S detik.
    """
    hit = _SHADOW.get("v")
    if hit and (datetime.now(timezone.utc).timestamp() - hit[0]) < SHADOW_TTL_S:
        return hit[1]
    out: dict[str, float] = {}
    try:
        p = build_panel()
        if not p.empty:
            d = prepare(p, training=False)
            if not d.empty:
                last = d["ts"].max()
                d = d[d["ts"] == last]
                if len(d) >= MIN_NAMES_PER_DAY:
                    s = pd.Series(score_m0(d), index=d["ticker"].values)
                    out = {t: round(float(v), 4) for t, v in s.rank(pct=True).items()}
    except Exception:  # noqa: BLE001 — bayangan tak boleh menjatuhkan jalur prediksi
        out = {}
    _SHADOW["v"] = (datetime.now(timezone.utc).timestamp(), out)
    return out


def shadow_for(ticker: str) -> float | None:
    """Persentil cross-sectional 1 saham (None kalau di luar universe tradeable hari ini)."""
    return shadow_scores().get(ticker)


def _self_check() -> None:
    """Harness harus bisa membedakan sinyal tertanam dari derau. Kalau tidak, semua
    angka di modul ini tak berarti."""
    rng = np.random.default_rng(1)
    rows = []
    for day in range(120):
        for k in range(60):
            f = rng.normal()
            noise = rng.normal()
            # target dipengaruhi f (sinyal nyata) + derau besar
            rows.append({"ts": f"2026-{day // 30 + 1:02d}-{day % 30 + 1:02d}",
                         "ticker": f"T{k}", "mom_comp": f, "_noise": noise,
                         "y_raw": 0.35 * f + rng.normal()})
    d = pd.DataFrame(rows)
    d["y"] = d.groupby("ts")["y_raw"].transform(_crank)
    real = ic_per_date(d, d["mom_comp"].values)
    fake = ic_per_date(d, d["_noise"].values)
    lo_r, _ = block_bootstrap(real.values)
    lo_f, hi_f = block_bootstrap(fake.values)
    assert real.mean() > 0.1, f"sinyal tertanam harus terdeteksi, dapat IC={real.mean():.3f}"
    assert lo_r > 0, "CI sinyal nyata harus di atas 0"
    assert lo_f < 0 < hi_f, f"derau harus tak signifikan, dapat CI=[{lo_f:.3f},{hi_f:.3f}]"
    p_fake = permutation_p(d, d["_noise"].values, float(fake.mean()), iters=200)
    assert p_fake > 0.05, f"permutasi harus menolak derau, p={p_fake}"
    p_real = permutation_p(d, d["mom_comp"].values, float(real.mean()), iters=200)
    assert p_real < 0.01, f"permutasi harus menerima sinyal nyata, p={p_real}"
    print("self-check OK: sinyal tertanam terdeteksi (IC "
          f"{real.mean():.3f}, p={p_real:.4f}), derau ditolak (p={p_fake:.3f})")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if "--self-check" in sys.argv:
        _self_check()
        raise SystemExit(0)
    from app import db
    db.init_db()
    r = run()
    print(json.dumps({k: v for k, v in r.items() if k != "folds"},
                     indent=2, ensure_ascii=False))
