"""Mesin keputusan heuristik (tanpa LLM) — cadangan saat USE_LLM=false / API gagal.

Mengkodekan aturan dari knowledge.py secara deterministik supaya sistem SELALU
menghasilkan prediksi & paper-trade, bahkan tanpa kuota API.
"""
from __future__ import annotations

import json

import config
from app import repo, tick


# Jepit kontribusi berita ke besaran SATU term teknikal. Sebelumnya `news` (dijepit ±5) dikali
# 0,8 → rentang ±4,0, sementara SELURUH sebaran skor teknikal yang benar-benar terjadi cuma
# -1,71..+1,69 (rata-rata per kuintil, n=16.281 likuid). Artinya satu tag berita impact -2
# sendirian (-1,6) sudah melewati ambang arah -1,0: berita mengalahkan semua teknikal digabung.
#
# Yang membuat itu tak bisa dipertahankan: tagnya TAK memprediksi apa pun. Diukur 2026-08-31
# atas 4.047 pasang berita-saham-tanggal, kelebihan return atas pasar di tanggal yang sama —
# selisih berpasangan per-tanggal antara berita positif dan negatif = -0,015 pp, CI 95%
# [-0,541, +0,449]. Skalanya juga tak monoton: impact -1 menghasilkan +0,301% (lebih baik
# daripada impact +2 yang cuma +0,008%). Sebabnya terlihat di data mentah — tagger menangkap
# penyebutan MEREK, bukan kabar penggerak harga ("Mauro Zijlstra gabung Arema FC" → BBRI,
# "PT Vale salurkan bantuan gempa NTT" → INCO).
#
# ponytail: menskala, bukan menolkan DAN bukan menjepit. Menolkan membuang katalis emiten yang
# nyata (rights issue, gagal bayar) bersama derau sponsor bola. Menjepit dgn min/max juga salah:
# pada cap 0,8 setiap |impact|>=1 langsung mentok, sehingga diskon priced-in (bobot 1/2 saat
# berita MENGEKOR gerakan besar) jadi tak terlihat sama sekali — ketahuan oleh
# test_priced_in_news_discount. Menskala per-satuan mempertahankan urutan & diskon itu.
# Naikkan lagi HANYA setelah tagger lolos uji yang sama dgn CI di atas nol.
NEWS_MAX = 0.8              # kontribusi maksimum (|impact| 5, berita memimpin)
_NEWS_UNIT = NEWS_MAX / 5.0  # per satu poin impact


def _news_sentiment(ticker: str) -> int:
    score = 0
    for n in repo.news_for_ticker(ticker, limit=8):
        score += int(n.get("impact") or 0)
    return max(-5, min(5, score))


def score_signals(feats: dict, news: int = 0) -> tuple[float, list[str]]:
    """Skor komposit MURNI dari fitur teknikal + sentimen berita.
    Fungsi deterministik — dipakai engine LIVE maupun BACKTEST (konsisten)."""
    score = 0.0
    factors: list[str] = []
    rsi = feats.get("rsi14", 50)
    consec_down = feats.get("consec_down", 0)
    consec_up = feats.get("consec_up", 0)
    cum3 = feats.get("cum_change_3d", 0)
    above_sma = feats.get("above_sma20", False)
    vol_ratio = feats.get("vol_vs_avg", 1)
    macd_hist = feats.get("macd_hist", 0)

    # Volume = konfirmasi minat beli. Rebound TANPA volume (vol<0.8x) = lemah/palsu (aturan #4)
    # → bonus oversold dibobot ½. Tanpa pembeli, "dip" cenderung lanjut jatuh, bukan mantul.
    low_vol = vol_ratio < 0.8
    # Falling knife = turun tajam + momentum negatif, ATAU masih di bawah SMA20 + RSI rendah +
    # volume kering (dip yang BELUM berhenti jatuh). Ambang -10 (dulu -12 kelewat dalam → SUPA
    # cum3 -11.5% lolos jadi 'rebound' bullish). Sinyal oversold di sini = GEJALA crash, bukan beli.
    knife = (cum3 <= -10 and macd_hist < 0) or (not above_sma and rsi < 32 and low_vol and cum3 <= -6)
    if knife:
        score -= 1.2
        factors.append(f"falling knife: turun {cum3}%/3hr, momentum/volume belum konfirmasi rebound")
    elif consec_down >= 3 and cum3 <= -5 and news >= 0:
        score += 1.0 if low_vol else 2.0
        factors.append(f"oversold rebound: turun {consec_down} hari ({cum3}%)"
                       + (" — volume tipis, bobot ½" if low_vol else ""))
    if rsi < 30 and not knife:
        score += 0.75 if low_vol else 1.5
        factors.append(f"RSI oversold ({rsi})" + (" — volume tipis, bobot ½" if low_vol else ""))
    elif rsi > 70:
        score -= 1.5
        factors.append(f"RSI overbought ({rsi})")
    if above_sma:
        score += 0.8
        factors.append("di atas SMA20 (uptrend)")
    else:
        score -= 0.6
        factors.append("di bawah SMA20 (downtrend)")
    if consec_up >= 4 and rsi > 65:
        score -= 1.0
        factors.append(f"naik {consec_up} hari beruntun, rawan koreksi")
    if vol_ratio > 2:
        factors.append(f"volume tinggi ({vol_ratio}x)")
        score += 0.4 if above_sma else -0.4
    elif low_vol and not above_sma:
        # turun + di bawah tren + volume kering = tesis rebound kurang bahan bakar (HOPE-type)
        score -= 0.5
        factors.append(f"volume tipis ({vol_ratio}x) di bawah SMA20 — rebound kurang bahan bakar")
    # MACD histogram (momentum)
    if feats.get("macd_pos") and macd_hist > 0:
        score += 0.6
        factors.append("MACD positif (momentum naik)")
    elif macd_hist < 0:
        score -= 0.5
        factors.append("MACD negatif (momentum turun)")
    # Momentum 10-hari — fitur #3 TERKUAT model terlatih (bobot +0.148, OOS AUC 0.63) yang DULU
    # diabaikan score_signals. Tren 10h searah = kelanjutan (bukan hanya SMA20 harian). Ambang ±8%
    # & bobot 0.6 (setara MACD); divalidasi backtest OOS (bukan grid-search test-set = anti-overfit).
    mom10 = feats.get("momentum_10d", 0)
    if mom10 >= 8:
        score += 0.6
        factors.append(f"momentum 10h kuat (+{mom10:.0f}%)")
    elif mom10 <= -8:
        score -= 0.6
        factors.append(f"momentum 10h negatif ({mom10:.0f}%)")
    # Bollinger %b (mean reversion)
    pb = feats.get("bollinger_pct_b", 0.5)
    if pb <= 0.05 and not knife:
        score += 0.8
        factors.append("sentuh band bawah Bollinger (oversold)")
    elif pb >= 0.95:
        score -= 0.8
        factors.append("sentuh band atas Bollinger (overbought)")
    # Support / resistance
    if feats.get("near_support") and not knife:
        score += 0.7
        factors.append("di area support (potensi mantul)")
    if feats.get("near_resistance") and vol_ratio < 2:
        score -= 0.5
        factors.append("di resistance tanpa volume (rawan tertahan)")
    # Divergence harga vs RSI
    if feats.get("bull_divergence"):
        score += 0.9
        factors.append("bullish divergence (RSI naik saat harga turun)")
    elif feats.get("bear_divergence"):
        score -= 0.9
        factors.append("bearish divergence")
    # Streak ARA/ARB — hindari falling knife & kejar ARA
    if feats.get("arb_days_5", 0) >= 1 and news < 0:
        score -= 0.6
        factors.append("baru kena ARB + berita negatif (falling knife)")
    if feats.get("ara_days_5", 0) >= 1:
        factors.append("baru ARA (momentum kuat tapi rawan koreksi)")
        score -= 0.3
    if news != 0:
        # Berita PRICED-IN: headline yang MENGEKOR gerakan besar searah (negatif SETELAH saham
        # sudah jatuh dalam / positif setelah melesat) = telat, gerakannya sudah terjadi.
        # Data 2026-07-02 (sejak 26-Jun): DOWN+berita-negatif 59% vs DOWN tanpa berita 76%;
        # yang sudah jatuh >=8%/3hr cuma 53%. Bobot dipangkas 1/2 saat mengekor, penuh saat memimpin.
        w = 0.5 if (news < 0 and cum3 <= -8) or (news > 0 and cum3 >= 8) else 1.0
        contrib = news * _NEWS_UNIT * w
        score += contrib
        factors.append(f"sentimen berita {news:+d} → skor {contrib:+.2f} (skala ±{NEWS_MAX})"
                       + (" — mengekor gerakan besar, bobot 1/2, rawan priced-in" if w < 1.0 else ""))
    return score, factors


def score_to_view(score: float) -> tuple[str, float, float, int]:
    """score -> (direction, probability, expected_pct, horizon_days).

    CATATAN (audit 2026-07-15): probabilitas MENTAH di sini SENGAJA formula (50+9|score|) —
    ini bukan klaim final. Kalibrasi jujur ke realisasi dikerjakan HILIR oleh
    skill.calibrated_probability (live) yang membaca resolved_predictions NYATA per-arah.
    Percobaan 2026-07-14 menjadikan prob=win-rate backtest DIBATALKAN: score_to_view dipakai
    JUGA di backtest → prob=output backtest sebelumnya = LOOP UMPAN-BALIK yang mematikan
    diagnostik kalibrasi (klaim≈realisasi by construction). Backtest WAJIB deterministik &
    independen (pola konsisten di seluruh modul ini)."""
    if score >= 1.0:
        direction = "UP"
    elif score <= -1.0:
        direction = "DOWN"
    else:
        direction = "FLAT"
    probability = round(max(35, min(85, 50 + abs(score) * 9)), 1)
    expected = round(score * 0.9, 2)
    horizon = 3 if direction != "FLAT" else 5
    return direction, probability, expected, horizon


def decide(ticker: str, quote: dict, position: dict | None, cash: float,
           force_horizon: int | None = None) -> dict:
    feats = json.loads(quote.get("features_json") or "{}") if quote else {}
    price = quote.get("price") or 0.0
    news = _news_sentiment(ticker)
    score, factors = score_signals(feats, news)

    # REZIM MAKRO (live-only; TIDAK di score_signals supaya backtest tetap deterministik,
    # tanpa look-ahead): briefing global semalam memiringkan keputusan. RISK-OFF kuat → tekan
    # skor (kurangi UP, perbanyak FLAT/DOWN); RISK-ON kuat → angkat.
    # ponytail: bobot tetap 0.25 & ambang ±1.5; kalibrasi via knowledge.py kalau perlu.
    macro_lean = 0.0
    try:
        from app.data import premarket  # lazy: hindari import berat di jalur backtest
        macro_lean = max(-3.0, min(3.0, premarket.global_brief().get("score", 0.0)))
    except Exception:  # noqa: BLE001
        pass
    if abs(macro_lean) >= 1.5:
        score += macro_lean * 0.25
        factors.append(f"rezim makro {'RISK-OFF' if macro_lean < 0 else 'RISK-ON'} "
                       f"(lean {macro_lean:+.1f}) → skor {macro_lean * 0.25:+.2f}")

    # FAKTOR STRUKTURAL / EVENT (live-only, named → dievaluasi per-faktor di laporan):
    # arus asing resmi + aksi korporasi/dividen dari filing. Bukan harga-TA (yg ~acak).
    fired_signals: list[dict] = []
    try:
        from app.agents import factors as _factors  # lazy: backtest tak sentuh ini
        score, factors, fired_signals = _factors.apply(ticker, feats, price, score, factors)
    except Exception:  # noqa: BLE001
        pass

    direction, probability, expected, horizon = score_to_view(score)
    # Horizon dipaksa (mis. companion 1-hari): skala ekspektasi & target ke jendela itu —
    # gerak 1-hari jauh lebih kecil dari 3-hari; 1-hari juga lebih bising → keyakinan diturunkan.
    if force_horizon and horizon:
        expected = round(expected * force_horizon / horizon, 2)
        if force_horizon == 1:
            probability = round(max(35.0, min(70.0, 50 + (probability - 50) * 0.7)), 1)
        horizon = force_horizon

    # TARGET BER-SKALA VOLATILITAS: gerak wajar saham ~ sigma_harian x sqrt(horizon) (difusi).
    # Formula lama (score*0.9) buta karakter saham — saham sigma 1%/hari diberi target sama
    # dgn sigma 5%/hari (akar 'target muluk' Aturan 72). Clamp |expected| <= 1.5*sigma*sqrt(h);
    # sigma ~0 (saham beku) → target ~0 → FLAT alami. Live-only (backtest tetap score_to_view).
    sigma = float(feats.get("volatility_20d") or 0.0)
    if sigma > 0 and direction != "FLAT":
        cap = round(1.5 * sigma * horizon ** 0.5, 2)
        if abs(expected) > cap:
            factors.append(f"target diklamp volatilitas: {expected:+.1f}%→{'+' if expected > 0 else '-'}{cap}% "
                           f"(sigma20d {sigma}%/hari, {horizon}h)")
            expected = cap if expected > 0 else -cap

    # MODEL-AGREEMENT GATE (tervalidasi 2026-06-26 atas 2.739 sampel historis: arah |score|
    # benar 59.8% saat SEPAKAT dgn model vs HANYA 39.8% saat KONFLIK — spread 20pt). Validasi itu
    # memakai model ber-OOS AUC 0,62; model.json SEKARANG ber-AUC 0,451 (di bawah acak) sehingga
    # `is_usable` membungkamnya dan SELURUH blok ini mati (pup=None). Statusnya tampil di
    # /api/skill lewat `model_status` — jangan simpulkan gate ini aktif dari komentar.
    # Konflik → arah teknikal TAK andal → FLAT. Sepakat → simpan p_dir utk diferensiasi keyakinan.
    p_dir = None
    try:
        from app import model as _model  # lazy
        pup = _model.predict_proba(feats)
    except Exception:  # noqa: BLE001
        pup = None
    if pup is not None and direction != "FLAT":
        model_dir = "UP" if pup >= 0.5 else "DOWN"
        if model_dir != direction:
            factors.append(f"KONFLIK score={direction} vs model={model_dir} (P_naik {pup:.0%}) "
                           f"→ FLAT (arah teknikal cuma ~40% benar saat tak selaras dgn model)")
            direction, expected, probability = "FLAT", 0.0, 53.0
        else:
            p_dir = pup if direction == "UP" else 1.0 - pup
            factors.append(f"model SEPAKAT {direction} (P={p_dir:.0%}) — sinyal selaras")

    # Keyakinan TER-KALIBRASI ke realisasi historis (data nyata), bukan cuma formula.
    raw_prob = probability          # dicatat mentah → bahan fit skill.KEEP_SPREAD nanti
    try:
        from app.agents import skill  # lazy: hindari circular import (backtest <-> heuristic)
        cal = skill.calibrated_probability(direction, probability)
        if abs(cal - probability) >= 1:
            factors.append(f"keyakinan dikalibrasi ke data nyata ({probability:.0f}%→{cal:.0f}%)")
        probability = cal
    except Exception:  # noqa: BLE001
        pass

    # DIFERENSIASI keyakinan dari KEKUATAN model saat sepakat — pulihkan informasi yg hilang
    # akibat kalibrasi yg meratakan semua ke ~satu angka (mis. semua 64%/74%). Plafon 80%
    # (rendah hati), lantai 50%. Model kuat → keyakinan naik; model dekat 0.5 → keyakinan turun.
    if p_dir is not None and direction in ("UP", "DOWN"):
        probability = round(min(80.0, max(50.0,
                            0.6 * probability + 0.4 * (50 + (p_dir - 0.5) * 80))), 1)

    # LIKUIDITAS (Aturan 71, ditegakkan deterministik): keyakinan TINGGI pada saham nyaris tak
    # diperdagangkan = tak andal. Data 2026-06-26: high-conf ≥80% di saham illikuid (<Rp1M
    # turnover) cuma menang 45% (vs 78% di likuid); miss sideways (ROTI/INTD/EDGE/BAPA) semua
    # turnover ~Rp0 (beku) tapi diklaim 80-85%. Cap keyakinan illikuid → kalibrasi jujur.
    turnover = price * (quote.get("volume") or 0.0)
    if turnover < 1e9 and probability > 65.0:        # < Rp1 miliar diperdagangkan = illikuid
        factors.append(f"illikuid (turnover Rp{turnover / 1e9:.2f}M) → keyakinan dibatasi "
                       f"{probability:.0f}%→65% (Aturan 71)")
        probability = 65.0

    # Ambang aksi RELATIF ke base rate pasar arah+horizon ini (app.eval.edge_floor), bukan
    # angka mutlak 60/66 yang lama — lihat config.MIN_EDGE_PP untuk alasan lengkapnya.
    risk_off = macro_lean <= -1.5
    edge_pp = config.MIN_EDGE_PP_RISKOFF if risk_off else config.MIN_EDGE_PP
    from app.eval import edge_floor as _edge_floor      # lazy: eval menarik numpy/pandas

    # Gate DOWN makin ketat saat RISK-ON — SIMETRIS dgn gate risk-off di bawah (Aturan 79).
    # Masalah nyata 30 Jun-3 Jul 2026: IHSG +3.3%/3hr, breadth 79%, asing net-buy, tapi engine
    # masih 68% DOWN (indikator ber-window lag di V-bottom) → DOWN win-rate runtuh 73%→28%
    # (n=136). Risk-on kuat: DOWN berkeyakinan lemah = sinyal lag, bukan tesis → FLAT.
    if macro_lean >= 1.5 and direction == "DOWN":
        down_floor = _edge_floor("DOWN", horizon, config.MIN_EDGE_PP_RISKOFF)
        if probability < down_floor:
            factors.append(f"rezim RISK-ON (lean {macro_lean:+.1f}) + keyakinan DOWN "
                           f"{probability:.0f}% < {down_floor:.0f}% (base rate {horizon}h "
                           f"+{config.MIN_EDGE_PP_RISKOFF:.0f} pp) → FLAT (indikator bearish "
                           f"lag di pembalikan, Aturan 79)")
            direction, expected, probability = "FLAT", 0.0, 53.0

    # Gate BUY makin ketat saat RISK-OFF: jangan menambah posisi melawan arus makro & jangan
    # habiskan kas saat panik (masalah nyata 2026-06-26: 21/25 prediksi UP padahal lean -4.6).
    prob_floor = _edge_floor("UP", horizon, edge_pp)
    exp_floor = 1.0 if risk_off else 0.6
    size_cap = 6.0 if risk_off else 12.0

    # GERBANG UANG: lengan UP wajib TERBUKTI di atas base rate pasar sebelum kas dibelanjakan.
    #
    # Kenapa lantai keyakinan saja TIDAK cukup (disimulasikan 2026-08-31 atas 16.281 sampel
    # likuid / 219 tanggal): lantai MUTLAK 60 membuat gerbang tertutup permanen (0 dari 3.883
    # sinyal UP lolos — inilah 5 hari bursa tanpa beli, 24-31 Agt). Menggantinya dgn lantai
    # relatif base rate memperbaiki satuannya, tapi lalu 3.883 dari 3.883 lolos: sakelar mati
    # permanen cuma bertukar jadi sakelar nyala permanen. Dan yang mengalir lewat SELURUH
    # rantai gerbang itu menghasilkan -0,373% per 5 hari sementara pasar +0,440% (n=459),
    # sejalan dgn vonis app.eval bahwa score_signals tak punya edge (CI [-0,749, +0,455]).
    # Jadi tak ada setelan ambang yang bisa menyelamatkannya; menurunkan ambang cuma
    # mempercepat kerugian.
    #
    # Yang benar: bukan angka pilihan tangan, melainkan BUKTI. Simetris dengan
    # orchestrator._proven_loss_gate (lengan terbukti RUGI berhenti bertaruh), lengan hanya
    # boleh membelanjakan kas setelah terbukti MENANG di atas base rate per-tanggal.
    # Gerbang ini MEMBUKA SENDIRI: prediksi UP tetap dicatat & dinilai walau tak ditradingkan,
    # jadi buktinya terus terkumpul dan begitu CI-nya positif pembelian jalan tanpa disentuh.
    action = "HOLD"
    size = 0.0
    arm_ok, arm_why = True, ""
    if direction == "UP":
        try:
            from app.agents import skill as _sk
            v = _sk.arm_verdict("UP", 2, side="above")
            arm_ok, arm_why = bool(v.get("pass")), v.get("detail", "")
        except Exception:  # noqa: BLE001 — vonis gagal → jangan blokir (perilaku lama)
            arm_ok = True
    if direction == "UP" and probability >= prob_floor and expected > exp_floor and arm_ok:
        action, size = "BUY", min(size_cap, 4 + abs(score) * 2)
        if risk_off:
            size *= 0.5  # ukuran setengah di pasar risk-off
    elif direction == "UP" and probability >= prob_floor and expected > exp_floor:
        factors.append(f"[gerbang uang] sinyal UP lolos keyakinan {probability:.1f}% >= "
                       f"{prob_floor:.1f}% & exp {expected:+.1f}%, TAPI lengan UP belum "
                       f"terbukti di atas base rate → tak ada beli. {arm_why}")
    elif direction == "DOWN" and position:
        action = "SELL"
    elif direction == "UP" and expected > exp_floor and probability < prob_floor:
        # SELURUH program beli mati di sini, dan sebelum 2026-08-24 TANPA SATU BARIS JEJAK pun.
        # DUA sebabnya sudah diperbaiki 2026-08-31: (a) keyakinan tak lagi satu skalar global
        # (skill.KEEP_SPREAD), (b) lantainya tak lagi angka mutlak 60 melainkan base rate pasar
        # + MIN_EDGE_PP. Jejak ini DIPERTAHANKAN: kalau program beli mati lagi, alasannya harus
        # terbaca dari satu baris, bukan disimpulkan dari nol trade selama seminggu.
        factors.append(f"[program beli TERTUTUP] UP exp {expected:+.1f}% lolos, tapi keyakinan "
                       f"terkalibrasi {probability:.1f}% < lantai {prob_floor:.1f}% "
                       f"(base rate UP {horizon}h + {edge_pp:.0f} pp edge minimum)")

    # Target ditarik ke fraksi harga IDX. Harga tak pernah singgah di antara tick, jadi target
    # berdesimal menilai sesuatu yang mustahil tersentuh, dan klaim yang lebih kecil dari satu
    # tick tak punya ruang sama sekali. Lihat app/tick.py.
    target, expected_real = tick.target_for(price, expected, direction)
    if target is not None:
        expected = expected_real

    return {
        "ticker": ticker,
        "direction": direction,
        "probability": round(probability, 1),
        "horizon_days": horizon,
        "expected_pct": expected,
        "target_price": target,
        "entry_price": price,
        "action": action,
        "term": "pendek",  # heuristik = teknikal murni → jangka pendek
        "size_pct": round(size, 1),
        "key_factors": factors,
        "signals": [{"name": s["name"], "dir": s["dir"]} for s in fired_signals],
        "critique": "(mode heuristik tanpa LLM)",
        "reasoning": f"skor komposit {round(score,2)} dari aturan teknikal+berita.",
        "_calibrated": True,  # sudah lewat skill.calibrated_probability di atas → _commit jangan shrink dua kali
        # BAYANGAN (dicatat, tak menyetir apa pun) — lihat collector di repo.save_prediction.
        "raw_prob": round(raw_prob, 1),
        "score": round(score, 3),
    }
