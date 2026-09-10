"""Cek gate volume + falling-knife di score_signals & cap likuiditas di decide()."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import json
from app.agents.heuristic import score_signals, score_to_view
from app.agents import skill as _skill

# Fungsi ASLI ditangkap SEBELUM test mana pun sempat menambalnya. `_isolate()` mengganti
# `skill.calibrated_probability` dengan stub dan tak pernah mengembalikannya, jadi test yang
# ingin menguji kalibrasi SUNGGUHAN harus memegang referensi ini — bukan membaca atribut modul.
# Ketahuan saat deploy 31 Agt: lolos di pytest tapi gagal di runner `__main__` karena urutan
# eksekusinya beda; nilai yang keluar (55,9) ternyata konstanta dari stub test sebelumnya.
_REAL_CALIB = _skill.calibrated_probability


def _f(**kw):
    base = dict(rsi14=50, consec_down=0, consec_up=0, cum_change_3d=0, above_sma20=True,
                vol_vs_avg=1.0, macd_hist=0.0, bollinger_pct_b=0.5)
    base.update(kw)
    return base


def test_falling_knife_caught():
    # SUPA-type: cum3 -11.5, RSI 25, di bawah SMA, volume kering → harus KNIFE (bukan rebound bullish)
    s, fac = score_signals(_f(cum_change_3d=-11.5, rsi14=25, above_sma20=False,
                              vol_vs_avg=0.2, macd_hist=-0.5, consec_down=4))
    assert any("falling knife" in f for f in fac), fac
    d, *_ = score_to_view(s)
    assert d != "UP", f"falling knife tak boleh UP, dapat {d} (score {s})"


def test_volume_confirms_rebound():
    # oversold SAMA, beda volume → versi volume-kering harus skor LEBIH RENDAH (rebound lemah)
    hi, _ = score_signals(_f(consec_down=3, cum_change_3d=-6, rsi14=28, above_sma20=False,
                             vol_vs_avg=1.5, macd_hist=0.5))
    lo, _ = score_signals(_f(consec_down=3, cum_change_3d=-6, rsi14=28, above_sma20=False,
                             vol_vs_avg=0.2, macd_hist=0.5))
    assert lo < hi, f"volume tipis harus menekan skor rebound: lo={lo} hi={hi}"


def test_healthy_uptrend_unaffected():
    # uptrend sehat + volume cukup → tetap bullish (gate tak merusak sinyal bagus)
    s, _ = score_signals(_f(above_sma20=True, vol_vs_avg=1.5, macd_hist=0.6, rsi14=55))
    assert s > 0, s


def _isolate():
    """Patch dependensi decide() agar uji deterministik (tanpa DB)."""
    from app.agents import heuristic as H
    from app.data import premarket
    from app.agents import skill
    H._news_sentiment = lambda t: 0
    premarket.global_brief = lambda: {"score": 0.0}
    skill.calibrated_probability = lambda d, p: p   # identity → uji logika, bukan kalibrasi
    return H


def _down_feats():
    return _f(cum_change_3d=-12, macd_hist=-1.0, rsi14=75, above_sma20=False, vol_vs_avg=1.0)


def test_illiquid_confidence_cap():
    # saham illikuid (turnover < Rp1 M) tak boleh diklaim keyakinan tinggi (Aturan 71)
    H = _isolate()
    import app.model as M
    M.predict_proba = lambda f: 0.20               # model SEPAKAT DOWN (agar tak FLAT krn konflik)
    quote = {"ticker": "TEST", "price": 100.0, "volume": 1000.0,  # turnover Rp100rb (illikuid)
             "features_json": json.dumps(_down_feats())}
    d = H.decide("TEST", quote, position=None, cash=1e7)
    assert d["direction"] == "DOWN", d["direction"]
    assert d["probability"] <= 65.0, f"illikuid harus di-cap <=65, dapat {d['probability']}"


def test_model_conflict_to_flat():
    # teknikal DOWN kuat TAPI model bilang UP → KONFLIK → FLAT (arah tak andal)
    H = _isolate()
    import app.model as M
    M.predict_proba = lambda f: 0.80               # model UP, lawan score DOWN
    quote = {"ticker": "TEST", "price": 5000.0, "volume": 1_000_000.0,  # likuid (turnover 5M)
             "features_json": json.dumps(_down_feats())}
    d = H.decide("TEST", quote, position=None, cash=1e7)
    assert d["direction"] == "FLAT", f"konflik harus FLAT, dapat {d['direction']}"


def test_model_agree_kept():
    # teknikal DOWN + model DOWN → arah dipertahankan (tak FLAT)
    H = _isolate()
    import app.model as M
    M.predict_proba = lambda f: 0.20
    quote = {"ticker": "TEST", "price": 5000.0, "volume": 1_000_000.0,
             "features_json": json.dumps(_down_feats())}
    d = H.decide("TEST", quote, position=None, cash=1e7)
    assert d["direction"] == "DOWN", f"sepakat harus DOWN, dapat {d['direction']}"


def test_risk_on_dampens_weak_down():
    """RISK-ON kuat (lean +2.5): DOWN berkeyakinan LEMAH = indikator lag di pembalikan -> FLAT
    (Aturan 79); DOWN berkeyakinan KUAT tetap DOWN (tesis kuat tak dibungkam).

    Ambangnya kini RELATIF ke base rate pasar (app.eval.edge_floor), bukan angka mutlak 66 —
    lihat config.MIN_EDGE_PP. Test memakai lantai yang dihitung, jadi pembaruan tabel base rate
    tak lagi bikin alarm palsu tapi regresi nyata (gate mati / gate selalu nyala) tetap kena.
    """
    import config
    from app.eval import edge_floor
    H = _isolate()
    from app.data import premarket
    from app.agents import skill
    import app.model as M
    premarket.global_brief = lambda: {"score": 2.5}
    # model DIBUNGKAM (kondisi live sejak 13 Agt 2026: model.json AUC 0,451 gagal gerbang).
    # Ini menguji GERBANG REZIM saja; interaksi dgn model sudah punya test sendiri
    # (test_model_conflict_to_flat / test_model_agree_kept). Dulu test ini memakai
    # predict_proba=0.20 sehingga blok diferensiasi model ikut mengangkat keyakinan dan
    # marginnya cuma 0,4 poin dari ambang — lulus karena kebetulan, bukan karena logika.
    M.predict_proba = lambda f: None
    quote = {"ticker": "TEST", "price": 1000.0, "volume": 5_000_000.0,
             "features_json": json.dumps(_down_feats())}
    floor = edge_floor("DOWN", 3, config.MIN_EDGE_PP_RISKOFF)
    skill.calibrated_probability = lambda d, p: floor - 5.0    # di BAWAH lantai
    d = H.decide("TEST", quote, position=None, cash=1e7)
    assert d["direction"] == "FLAT", d
    assert any("Aturan 79" in f for f in d["key_factors"]), d["key_factors"]
    skill.calibrated_probability = lambda d, p: floor + 5.0    # di ATAS lantai
    d2 = H.decide("TEST", quote, position=None, cash=1e7)
    assert d2["direction"] == "DOWN", d2["direction"]
    # lantai wajib DAPAT DICAPAI: gerbang yang tak pernah bisa dilewati bukan gerbang,
    # melainkan sakelar mati permanen (regresi nyata 24-31 Agt 2026).
    assert floor <= 85.0, floor


def test_priced_in_news_discount():
    # berita negatif SETELAH jatuh dalam (mengekor) = bobot 1/2; saat memimpin = penuh.
    ekor, fac = score_signals(_f(cum_change_3d=-9, above_sma20=False, rsi14=40), news=-3)
    pimpin, _ = score_signals(_f(cum_change_3d=-3, above_sma20=False, rsi14=40), news=-3)
    assert ekor > pimpin, f"berita mengekor harus didiskon: ekor={ekor} pimpin={pimpin}"
    assert any("mengekor" in f for f in fac), fac


def _up_feats():
    return _f(above_sma20=True, macd_hist=0.8, macd_pos=True, rsi14=58, vol_vs_avg=1.6,
              momentum_10d=9, volatility_20d=2.0)


def test_program_beli_tertutup_terjejak():
    """Sinyal UP layak beli TAPI keyakinan terkalibrasi di bawah lantai -> HOLD dengan JEJAK.

    Regresi nyata 14-20 & 24-31 Agt 2026: keyakinan terkalibrasi arah UP (satu skalar global
    untuk semua saham) duduk di 57,3-59,6 sementara lantai beli MUTLAK 60 -> NOL pembelian
    berhari-hari tanpa satu baris pun yang menjelaskan kenapa. Test ini GAGAL pada perilaku
    lama (tak ada penanda) dan memastikan alasannya selalu tercatat di key_factors.
    """
    import config
    from app.eval import edge_floor
    H = _isolate()
    from app.agents import skill
    import app.model as M
    M.predict_proba = lambda f: None                # model dibungkam (kondisi live sekarang)
    quote = {"ticker": "TEST", "price": 5000.0, "volume": 1_000_000.0,
             "features_json": json.dumps(_up_feats())}
    floor = edge_floor("UP", 3, config.MIN_EDGE_PP)
    skill.arm_verdict = lambda *a, **k: {"pass": True, "detail": "(stub: lengan terbukti)"}

    skill.calibrated_probability = lambda d, p: floor - 2.0    # di BAWAH lantai
    d = H.decide("TEST", quote, position=None, cash=1e7)
    assert d["direction"] == "UP", d["direction"]
    assert d["action"] == "HOLD", d["action"]
    assert any("program beli TERTUTUP" in f for f in d["key_factors"]), d["key_factors"]

    skill.calibrated_probability = lambda d, p: floor + 2.0    # di ATAS lantai -> beli jalan lagi
    d2 = H.decide("TEST", quote, position=None, cash=1e7)
    assert d2["action"] == "BUY", d2
    assert not any("program beli TERTUTUP" in f for f in d2["key_factors"]), d2["key_factors"]


def test_gerbang_uang_butuh_lengan_terbukti():
    """Sinyal UP yang lolos SEMUA gerbang keyakinan tetap TIDAK membelanjakan kas selama
    lengan UP belum terbukti di atas base rate pasar — dan alasannya tercatat.

    Disimulasikan 2026-08-31 atas 16.281 sampel likuid: lantai MUTLAK 60 meloloskan 0 dari
    3.883 sinyal UP (gerbang mati permanen), sedangkan lantai relatif base rate meloloskan
    3.883 dari 3.883 (gerbang nyala permanen). Keduanya sama-sama bukan penilaian. Yang
    lolos seluruh rantai gerbang menghasilkan -0,373%/5h vs pasar +0,440%, jadi membuka
    keran lewat setelan ambang = mempercepat rugi. Syaratnya harus BUKTI, dan bukti itu
    membuka gerbangnya sendiri karena prediksi tetap dicatat walau tak ditradingkan.
    """
    import config
    from app.eval import edge_floor
    H = _isolate()
    from app.agents import skill
    import app.model as M
    M.predict_proba = lambda f: None
    quote = {"ticker": "TEST", "price": 5000.0, "volume": 1_000_000.0,
             "features_json": json.dumps(_up_feats())}
    skill.calibrated_probability = lambda d, p: edge_floor("UP", 3, config.MIN_EDGE_PP) + 5.0

    skill.arm_verdict = lambda *a, **k: {"pass": False, "detail": "23 tanggal, CI memuat 0"}
    d = H.decide("TEST", quote, position=None, cash=1e7)
    assert d["direction"] == "UP", d["direction"]
    assert d["action"] == "HOLD", d["action"]
    assert any("gerbang uang" in f for f in d["key_factors"]), d["key_factors"]

    skill.arm_verdict = lambda *a, **k: {"pass": True, "detail": "terbukti DI ATAS baseline"}
    d2 = H.decide("TEST", quote, position=None, cash=1e7)
    assert d2["action"] == "BUY", d2
    assert not any("gerbang uang" in f for f in d2["key_factors"]), d2["key_factors"]


def test_keyakinan_tidak_runtuh_jadi_satu_angka():
    """Dua saham dengan sinyal BERBEDA wajib keluar dengan keyakinan BERBEDA.

    Ini invarian yang runtuh diam-diam 2026-08: skill.calibrated_probability pada n>=30
    membuang nilai mentah dan memakai win-rate arah (satu skalar), sehingga 105 prediksi UP
    di satu hari cuma punya 2 nilai keyakinan dan seluruh lengan DOWN punya 1. Akibatnya
    setiap ambang aksi berubah jadi sakelar ON/OFF global. Test memakai calibrated_probability
    ASLI (bukan identity) supaya regresi itu tertangkap di sini, bukan di produksi.
    """
    from app import repo
    # riwayat sintetis: 60 outcome arah UP, menang 40% → w=1,0 (bobot realisasi penuh)
    repo.resolved_predictions = lambda *_: [
        {"direction": "UP", "probability": 60, "outcome": "win" if i < 24 else "loss"}
        for i in range(60)]
    lemah = _REAL_CALIB("UP", 55.0)      # fungsi ASLI, kebal stub test lain
    kuat = _REAL_CALIB("UP", 80.0)
    assert kuat > lemah, f"sinyal kuat harus > sinyal lemah, dapat {kuat} vs {lemah}"
    assert kuat - lemah >= 5.0, f"sebaran tergerus habis: {lemah} -> {kuat}"
    # tapi rata-ratanya tetap JUJUR ke realisasi (tak boleh balik jadi overconfident 80%)
    assert kuat < 70.0, kuat


if __name__ == "__main__":
    test_falling_knife_caught()
    test_volume_confirms_rebound()
    test_healthy_uptrend_unaffected()
    test_illiquid_confidence_cap()
    test_model_conflict_to_flat()
    test_model_agree_kept()
    test_priced_in_news_discount()
    test_risk_on_dampens_weak_down()
    test_program_beli_tertutup_terjejak()
    test_gerbang_uang_butuh_lengan_terbukti()
    test_keyakinan_tidak_runtuh_jadi_satu_angka()
    print("OK test_heuristic: knife, volume, uptrend, illikuid-cap, konflik->FLAT, agree->arah, "
          "berita-mengekor didiskon, risk-on redam DOWN lemah, program-beli-tertutup terjejak, "
          "gerbang uang butuh lengan terbukti, keyakinan tak runtuh jadi satu angka.")
