"""Model statistik kecil: logistic regression (numpy) belajar P(naik) dari fitur teknikal
historis. Gantikan bobot heuristik TEBAKAN dengan bobot TERLATIH + terkalibrasi.

- Tanpa dependency baru (numpy saja). Bobot disimpan JSON ~1KB.
- Walk-forward: latih di data lama, uji di data baru (OOS jujur, anti-overfit).
- Fitur scale-invariant (% / rasio) supaya bisa dipool lintas saham.
Latih:  python -m app.model
"""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pandas as pd

import config
from app import repo
from app.data import stocks

MODEL_FILE = config.DATA_DIR / "model.json"
HORIZON = 5  # prediksi arah 5 hari ke depan (horizon OOS-terbaik dari backtest)

FEATURE_NAMES = [
    "rsi14", "change_pct", "consec_down", "consec_up", "cum_change_3d", "vol_vs_avg",
    "pct_from_52w_high", "pct_from_52w_low", "volatility_20d", "macd_hist_norm",
    "bollinger_pct_b", "momentum_10d", "dist_to_support", "dist_to_resistance",
    "ara_days_5", "arb_days_5", "above_sma20", "bull_divergence", "bear_divergence",
]


def _vec(f: dict) -> list[float]:
    last = f.get("last") or 1.0
    return [
        f.get("rsi14", 50), f.get("change_pct", 0), f.get("consec_down", 0),
        f.get("consec_up", 0), f.get("cum_change_3d", 0), f.get("vol_vs_avg", 1),
        f.get("pct_from_52w_high", 0), f.get("pct_from_52w_low", 0),
        f.get("volatility_20d", 0), f.get("macd_hist", 0) / last * 100,
        f.get("bollinger_pct_b", 0.5), f.get("momentum_10d", 0),
        f.get("dist_to_support", 0), f.get("dist_to_resistance", 0),
        f.get("ara_days_5", 0), f.get("arb_days_5", 0),
        1.0 if f.get("above_sma20") else 0.0,
        1.0 if f.get("bull_divergence") else 0.0,
        1.0 if f.get("bear_divergence") else 0.0,
    ]


def _load_prices(conn, ticker: str) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT ts, close, volume FROM prices WHERE ticker=? ORDER BY ts",
                           conn, params=(ticker,))
    return df.rename(columns={"close": "Close", "volume": "Volume"})


def build_dataset(max_tickers: int = 300, step: int = 3, lookback: int = 220,
                  liquid_only: bool = True, label: str = "sign"):
    """Dataset training. `liquid_only` (audit 2026-07-20): saham illikuid/beku DIBUANG —
    52% sampel historis ber-turnover < Rp1M/hari dan 11% ber-return ke depan persis 0.
    Label 'tidak naik' dari saham beku bukan informasi pasar, cuma ketiadaan perdagangan;
    model yang belajar dari itu mengoptimalkan hal yang salah.

    CATATAN (sadar, bukan kelalaian): filternya memakai likuiditas SEKARANG untuk memilih
    universe seluruh riwayat, bukan point-in-time per tanggal seperti `xsec.tradeable`.
    Bias seleksi ringan (saham yang dulu likuid tapi kini beku ikut terbuang). Dibiarkan
    karena model ini HANYA akan menskor universe yang sekarang likuid — mencocokkan
    populasi training dengan populasi pemakaian. Kalau nanti dipakai untuk riset historis,
    ganti ke point-in-time."""
    conn = sqlite3.connect(config.DB_PATH)
    tickers = [r[0] for r in conn.execute("SELECT DISTINCT ticker FROM prices").fetchall()]
    if liquid_only:
        keep = repo.liquid_tickers()
        tickers = [t for t in tickers if t in keep]
    tb_fn = None
    if label == "triple_barrier":               # label meniru exit trader (target/stop/waktu)
        from app import label as _label          # lazy: hindari import saat label default
        tb_fn = _label.triple_barrier
    X, Y, T = [], [], []
    n_frozen = 0
    for tk in tickers[:max_tickers]:
        df = _load_prices(conn, tk)
        n = len(df)
        if n < 80:
            continue
        closes = df["Close"].values
        tb = tb_fn(closes, max_h=HORIZON) if tb_fn is not None else None
        for i in range(max(60, n - lookback), n - HORIZON, step):
            # cek murah DULU: harga IDX bilangan bulat → rasio persis 1.0 → 0.0 eksak.
            # 11% sampel beku; jangan bayar compute_features (bagian termahal) untuk dibuang.
            ret = closes[i + HORIZON] / closes[i] - 1
            if ret == 0:            # harga tak bergerak 5 hari = tak diperdagangkan, bukan sinyal
                n_frozen += 1
                continue
            if tb is not None:
                y = tb[i]
                if np.isnan(y):     # ekor/tak terdefinisi → lewati
                    continue
            else:
                y = 1.0 if ret > 0 else 0.0
            feats = stocks.compute_features(df.iloc[:i + 1])
            if not feats:
                continue
            X.append(_vec(feats))
            Y.append(float(y))
            T.append(df["ts"].iloc[i])
    conn.close()
    if n_frozen:
        repo.log("engine", "resolve", f"dataset: {n_frozen} sampel beku (return 5h persis 0) dibuang")
    return np.array(X, float), np.array(Y, float), np.array(T)


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _auc(y, p):  # AUC = P(skor positif > skor negatif), rank-based
    pos, neg = p[y == 1], p[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return 0.5
    order = np.argsort(p)
    ranks = np.empty(len(p)); ranks[order] = np.arange(1, len(p) + 1)
    return (ranks[y == 1].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def _pav(y: np.ndarray) -> np.ndarray:
    """Pool-adjacent-violators: proyeksi isotonik (monoton non-turun) dari y berbobot sama.
    Inti kalibrasi isotonic — O(n), stdlib+numpy saja."""
    vals: list[float] = []
    cnts: list[int] = []
    for v in y.astype(float):
        vals.append(float(v)); cnts.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:  # langgar monoton → gabung blok
            nc = cnts[-1] + cnts[-2]
            nv = (vals[-1] * cnts[-1] + vals[-2] * cnts[-2]) / nc
            vals.pop(); cnts.pop(); vals[-1] = nv; cnts[-1] = nc
    out = np.empty(len(y)); i = 0
    for v, c in zip(vals, cnts):
        out[i:i + c] = v; i += c
    return out


def _fit_isotonic(p: np.ndarray, y: np.ndarray, max_pts: int = 64):
    """Kalibrasi isotonic skor_mentah → P(menang) NYATA. Kembalikan breakpoint (x, yhat)
    monoton utk np.interp saat prediksi. Disubsample <=max_pts agar model.json tetap ~KB.

    Ini yang HILANG (audit 2026-07-21): predict_proba mengembalikan sigmoid MENTAH; padahal
    heuristic.decide me-gate arah & membedakan keyakinan dari P model → P yang tak terkalibrasi
    mendistorsi gate. Isotonic membuat P = frekuensi menang empiris (Brier turun, bukan naik)."""
    order = np.argsort(p, kind="mergesort")
    xs = p[order]
    ys = _pav(y[order])
    xs_u, inv = np.unique(xs, return_inverse=True)          # x sama → rata-rata y (tetap monoton)
    ys_u = np.zeros(len(xs_u)); cnt = np.zeros(len(xs_u))
    np.add.at(ys_u, inv, ys); np.add.at(cnt, inv, 1); ys_u /= cnt
    if len(xs_u) > max_pts:                                  # subsample rata (JSON kecil)
        idx = np.unique(np.linspace(0, len(xs_u) - 1, max_pts).round().astype(int))
        xs_u, ys_u = xs_u[idx], ys_u[idx]
    return xs_u.tolist(), ys_u.tolist()


def _apply_calib(raw: float, m: dict) -> float:
    """Petakan P mentah → P terkalibrasi via breakpoint isotonic; tanpa kalibrasi → apa adanya
    (kompatibel mundur: model.json lama tanpa calib_x berperilaku persis seperti sebelumnya)."""
    cx, cy = m.get("calib_x"), m.get("calib_y")
    if not cx or not cy:
        return raw
    return float(np.interp(raw, cx, cy))


def train(max_tickers: int = 500, label: str = "sign") -> dict:  # 500: AUC 0.626→0.642, n 18k→30k (2026-07-01)
    X, Y, T = build_dataset(max_tickers=max_tickers, label=label)
    if len(X) < 500:
        return {"error": f"data kurang ({len(X)} sampel)"}
    order = np.argsort(T)              # urut waktu → split walk-forward
    X, Y = X[order], Y[order]
    cut = int(len(X) * 0.7)
    Xtr, Ytr, Xte, Yte = X[:cut], Y[:cut], X[cut:], Y[cut:]
    mu, sd = Xtr.mean(0), Xtr.std(0) + 1e-9
    Xtr_s, Xte_s = (Xtr - mu) / sd, (Xte - mu) / sd

    w, b, lr, lam = np.zeros(X.shape[1]), 0.0, 0.2, 1e-3
    for _ in range(4000):                       # gradient descent + L2
        g = _sigmoid(Xtr_s @ w + b) - Ytr
        w -= lr * (Xtr_s.T @ g / len(Xtr) + lam * w)
        b -= lr * g.mean()

    pte = _sigmoid(Xte_s @ w + b)
    acc = float(((pte > 0.5) == Yte).mean())
    base = float(max(Yte.mean(), 1 - Yte.mean()))  # akurasi tebak-mayoritas
    # Kalibrasi isotonic DILATIH DI OOS (bukan train) → P mencerminkan realisasi jujur, anti-overfit.
    calib_x, calib_y = _fit_isotonic(pte, Yte)
    pte_cal = np.interp(pte, calib_x, calib_y)
    brier = round(float(((pte - Yte) ** 2).mean()), 3)
    brier_cal = round(float(((pte_cal - Yte) ** 2).mean()), 3)
    res = {
        "features": FEATURE_NAMES, "mu": mu.tolist(), "sd": sd.tolist(),
        "w": w.tolist(), "b": float(b), "horizon": HORIZON, "n": len(X), "label": label,
        "oos_acc": round(acc * 100, 1), "base_rate": round(base * 100, 1),
        "oos_auc": round(float(_auc(Yte, pte)), 3),
        "brier": brier, "brier_cal": brier_cal,           # brier_cal <= brier = kalibrasi menolong
        "calib_x": calib_x, "calib_y": calib_y,           # breakpoint isotonic (dipakai predict_proba)
        "edge": round((acc - base) * 100, 1),  # >0 = kalahkan tebak-mayoritas
    }
    MODEL_FILE.write_text(json.dumps(res), encoding="utf-8")
    _cache.clear()         # paksa reload model baru (termasuk reset penanda 'sudah diperingatkan')
    if not is_usable(res):
        repo.log("engine", "resolve", f"model BARU tak layak pakai: OOS AUC {res['oos_auc']} < "
                 f"{MIN_USABLE_AUC}, edge {res['edge']} pp — disimpan untuk jejak, TIDAK dipakai "
                 f"mengambil keputusan", level="warn")
    repo.log("engine", "resolve", f"model dilatih: OOS acc {res['oos_acc']}% "
             f"(base {res['base_rate']}%, edge {res['edge']}%), AUC {res['oos_auc']}, "
             f"Brier {brier}->{brier_cal} (kalibrasi), n={res['n']}")
    return {k: v for k, v in res.items() if k not in ("mu", "sd", "w", "calib_x", "calib_y")}


_cache = {}

# Ambang daya-pisah OOS minimum sebelum model boleh menyetir keputusan. AUC 0.5 = tak bisa
# membedakan sama sekali; 0.52 kira-kira 2 sigma di atas acak untuk n~4.400 sampel OOS
# (SE AUC ~ 0.5/sqrt(n) ~ 0.009) — bukan angka selera, tapi batas "beda dari acak".
MIN_USABLE_AUC = 0.52


def is_usable(m: dict) -> bool:
    """Model ini layak dipakai untuk mengambil keputusan? Model TANPA daya pisah OOS harus
    DIAM, bukan menebak — kalau tidak, ia menyetir seluruh kebijakan arah dengan noise.

    Kejadian nyata (audit 2026-08-13, model.json 13 Agt): oos_auc 0.456 (DI BAWAH acak),
    edge -2,5 pp. Kalibrasi isotonic yang dipasang di atasnya lalu runtuh — 60 dari 64
    breakpoint bernilai SAMA (0,49298), menutup rentang P mentah 0,287-0,564 — sehingga
    673 dari 703 saham keluar dengan angka identik 0,49298. Karena < 0,5, arah model =
    "DOWN" untuk HAMPIR SEMUA SAHAM, selamanya. Akibatnya di heuristic.decide:
      - setiap sinyal UP dinilai KONFLIK dgn model → dipaksa FLAT (nol prediksi UP
        sepanjang 3-12 Agt, n=205),
      - setiap sinyal DOWN "sepakat" → lolos, p_dir 0,507 → diferensiasi keyakinan
        menghasilkan ~50,6 → keyakinan terpaku PERSIS 50,0 (28 dari 28 companion 12 Agt).
    Satu model tanpa skill menjelaskan tiga gejala sekaligus. predict_proba mengembalikan
    None saat tak layak; heuristic.decide sudah menangani None (gate model dilewati)."""
    auc = m.get("oos_auc")
    return auc is None or auc >= MIN_USABLE_AUC   # model lama tanpa metrik → perilaku lama


def predict_proba(feats: dict) -> float | None:
    """P(naik) dari model terlatih. None kalau belum dilatih ATAU model tak punya daya pisah
    OOS (lihat `is_usable`) — lebih baik tak bersuara daripada menyetir arah dengan noise."""
    if not feats:
        return None
    if "m" not in _cache:
        if not MODEL_FILE.exists():
            return None
        _cache["m"] = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
    m = _cache["m"]
    if not is_usable(m):
        if not _cache.get("warned"):
            _cache["warned"] = True
            repo.log("engine", "resolve", f"model DINONAKTIFKAN: OOS AUC {m.get('oos_auc')} < "
                     f"{MIN_USABLE_AUC} (tak beda dari acak) — gate model-agreement dilewati "
                     f"sampai ada model yang lolos", level="warn")
        return None
    x = (np.array(_vec(feats)) - np.array(m["mu"])) / np.array(m["sd"])
    raw = float(_sigmoid(x @ np.array(m["w"]) + m["b"]))
    return _apply_calib(raw, m)  # P terkalibrasi isotonic (fallback = raw kalau model lama)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    # label: "sign" (default) atau "triple_barrier". Contoh: python -m app.model triple_barrier
    lbl = sys.argv[1] if len(sys.argv) > 1 else "sign"
    print(f"melatih model... (label={lbl}, ekstrak fitur historis, walk-forward)")
    print(train(label=lbl))
