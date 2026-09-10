"""Cek target tercapai sebelum horizon habis -> tutup lebih awal (win) & analisis ulang
(orchestrator._resolve_target_hits). Lihat memory saham-idx-prediction ronde "target-hit awal"."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from app import repo
from app.agents import orchestrator as orch


def test_target_hit_closes_early_and_reanalyzes():
    # ts = intraday sesi bursa terakhir yang SUDAH tutup → bracket boleh aktif (guard hanya
    # cegah penilaian sebelum ada closing baru; prediksi lintas-sesi tetap dinilai target/adverse).
    # BUKAN "2 hari kalender lalu": kalau jatuh di akhir pekan harga belum bergerak sama sekali
    # (harga acuan == entry) dan penilaian menang/kalah jadi palsu — lihat cal.has_new_session.
    from datetime import datetime, time, timezone
    from app import market_calendar as cal
    _past = datetime.combine(cal.last_closed_session(datetime.now(timezone.utc)),
                             time(10, 0), tzinfo=cal.WIB).isoformat()
    preds = [
        # DOWN, target 1.8, harga sekarang 1.725 (SUDAH tembus target) -> resolve WIN
        {"id": 1, "ticker": "SMMT", "direction": "DOWN", "entry_price": 2.0, "ts": _past,
         "target_price": 1.8, "horizon_days": 3, "probability": 70, "reasoning": "x"},
        # UP, target 540 (+5.1% dari 514), harga 480 = -6.6% LAWAN >= jarak-target -> LOSS awal
        {"id": 2, "ticker": "AAAA", "direction": "UP", "entry_price": 514, "ts": _past,
         "target_price": 540, "horizon_days": 3, "probability": 70, "reasoning": "x"},
        # UP, target 520 (+4% dari 500), harga 495 = -1% lawan < jarak-target 4% -> tetap open
        {"id": 3, "ticker": "BBBB", "direction": "UP", "entry_price": 500, "ts": _past,
         "target_price": 520, "horizon_days": 3, "probability": 70, "reasoning": "x"},
    ]
    quotes = {"SMMT": {"price": 1.725}, "AAAA": {"price": 480}, "BBBB": {"price": 495}}
    resolved_calls = []
    reanalyzed = []

    repo.open_predictions = lambda: preds
    repo.get_quote = lambda t: quotes.get(t)
    repo.resolve_prediction = lambda pid, pct, outcome: resolved_calls.append((pid, pct, outcome))
    repo.add_lesson = lambda *a, **k: None
    repo.log = lambda *a, **k: None
    orch.run_one = lambda t, **k: reanalyzed.append(t)  # terima note=/horizon= kwarg

    n = orch._resolve_target_hits()

    assert n == 2, n
    assert (1, -13.75, "win") in resolved_calls, resolved_calls      # target tembus -> win
    assert any(pid == 2 and o == "loss" for pid, _, o in resolved_calls), resolved_calls  # lawan arah -> loss awal
    assert reanalyzed == ["SMMT", "AAAA"], reanalyzed                # keduanya dianalisis ulang
    assert not any(pid == 3 for pid, _, _ in resolved_calls)         # BBBB tetap open


def test_same_day_prediction_not_bracket_closed():
    """Permintaan user 2026-07-13: status jangan salah sebelum hari berakhir. Prediksi yang
    DIBUAT HARI INI tak boleh ditutup bracket (menang/kalah) oleh gerakan intraday — noise."""
    from datetime import datetime, timezone
    from app import repo
    from app.agents import orchestrator as orch
    today = datetime.now(timezone.utc).isoformat()
    # UP, adverse besar (-10%) HARI INI — tanpa guard would resolve loss; dgn guard tetap open
    preds = [{"id": 9, "ticker": "TDAY", "direction": "UP", "entry_price": 100.0, "ts": today,
              "target_price": 104.0, "horizon_days": 3, "probability": 70, "reasoning": "x"}]
    resolved_calls = []
    repo.open_predictions = lambda: preds
    repo.get_quote = lambda t: {"price": 90.0}
    repo.resolve_prediction = lambda pid, pct, o: resolved_calls.append(pid)
    repo.add_lesson = lambda *a, **k: None
    repo.log = lambda *a, **k: None
    orch.run_one = lambda t, **k: None
    n = orch._resolve_target_hits()
    assert n == 0 and resolved_calls == [], (n, resolved_calls)   # tak ditutup di hari sama
    print("same-day: prediksi hari-ini TIDAK ditutup bracket (status tetap berjalan) ok")


def test_bracket_symmetric_below_floor():
    """Audit 2026-07-27: target rapat (<2%) dulu menang saat harga menyentuh target tapi baru
    kalah pada -2% (ADVERSE_FLOOR) → ambang menang lebih dekat = win-rate menggelembung.
    Sekarang SATU jarak untuk dua sisi: +1.2% (di bawah lantai) tak menutup apa pun, dan
    gerak sebesar lantai menutup SIMETRIS di kedua arah."""
    from datetime import datetime, time, timezone
    from app import market_calendar as cal
    _past = datetime.combine(cal.last_closed_session(datetime.now(timezone.utc)),
                             time(10, 0), tzinfo=cal.WIB).isoformat()
    # target +1.2% (rapat, di bawah ADVERSE_FLOOR 2%)
    base = {"direction": "UP", "entry_price": 100.0, "ts": _past, "target_price": 101.2,
            "horizon_days": 3, "probability": 70, "reasoning": "x"}
    preds = [{**base, "id": 1, "ticker": "NEAR"},     # +1.2% searah: dulu WIN, kini tetap open
             {**base, "id": 2, "ticker": "PLUS"},     # +2.0% searah -> win
             {**base, "id": 3, "ticker": "MINUS"}]    # -2.0% lawan  -> loss (jarak sama persis)
    quotes = {"NEAR": {"price": 101.2}, "PLUS": {"price": 102.0}, "MINUS": {"price": 98.0}}
    calls = []
    repo.open_predictions = lambda: preds
    repo.get_quote = lambda t: quotes.get(t)
    repo.resolve_prediction = lambda pid, pct, o: calls.append((pid, o))
    repo.add_lesson = lambda *a, **k: None
    repo.log = lambda *a, **k: None
    orch.run_one = lambda t, **k: None

    orch._resolve_target_hits()

    assert (1, "win") not in calls, calls          # sentuh target rapat != menang instan
    assert not any(pid == 1 for pid, _ in calls), calls
    assert (2, "win") in calls and (3, "loss") in calls, calls   # ambang menang == ambang kalah


def test_target_derived_from_expected():
    """Target LLM yang bertengkar dgn expected_pct (AKRA: exp 1.0% tapi target +11.8%) dulu
    menang, sehingga bracket menutup di jarak 10x lipat dari yang ditampilkan. Kini target
    diturunkan dari expected_pct, lalu ditarik ke fraksi harga IDX supaya benar-benar bisa
    tersentuh: 1450 ada di pita tick Rp5, jadi 1464,5 dibulatkan naik ke 1465."""
    from app.agents import trader
    d = trader._sanitize("AKRA", {"direction": "UP", "expected_pct": 1.0,
                                  "target_price": 1621.07, "probability": 65,
                                  "horizon_days": 5}, {"price": 1450.0})
    assert d["target_price"] == 1465, d["target_price"]
    assert d["target_price"] % 5 == 0, "target wajib di grid tick"
    # expected_pct WAJIB ikut target yang sudah dibulatkan, bukan angka klaim aslinya.
    assert abs((d["target_price"] / 1450 - 1) * 100 - d["expected_pct"]) < 0.01


def test_commit_skips_duplicate_open_prediction():
    """Sinyal sama (ticker+arah) yang masih open TIDAK dicatat ulang (anti-inflasi statistik)."""
    saved = []
    acted = []
    from app.data import premarket, flow
    premarket.global_brief = lambda: {"score": 0.0}   # netral: gate rezim inert, uji dedup murni
    flow.ihsg_uptrend = lambda: (False, 0.0, 0.0)     # trend-gate inert (last=0)
    flow.local_reversal = lambda: (False, "")         # gate rezim inert (jangan baca DB nyata)
    repo.open_prediction_id = lambda t, d, h=None: 42 if (t, d) == ("FORU", "DOWN") else None
    repo.open_predictions_for_ticker = lambda t: []   # supersede: tak ada main lama utk ticker uji
    repo.save_prediction = lambda p: saved.append(p) or 99
    repo.log = lambda *a, **k: None
    repo.get_quote = lambda t: None
    # UP butuh pilar non-teknikal (evidence gate) — tanpa stub ini, hasil bergantung pada
    # berita REAL live (flaky, ikut jam bursa). "signals" struktural = pilar sah & stabil.
    repo.news_for_ticker = lambda *a, **k: []
    # Kalibrasi + gate keyakinan membaca riwayat NYATA — tanpa stub, keyakinan 65 ikut ditarik
    # ke realisasi DB live dan bisa jatuh <=50 (jadi FLAT) → test dedup ini flaky mengikuti data.
    repo.resolved_predictions = lambda *a, **k: []
    orch.paper.act_on_decision = lambda d, pid: acted.append(pid)

    d = {"ticker": "FORU", "direction": "DOWN", "probability": 65, "horizon_days": 3,
         "entry_price": 100, "target_price": 95, "expected_pct": -5, "reasoning": "x",
         "key_factors": [], "critique": "", "_analyst_view": None}
    pid = orch._commit(d)
    assert pid == 42 and not saved and acted == [42]   # skip insert, trade tetap dievaluasi

    d2 = {**d, "ticker": "BBCA", "direction": "UP",
          "signals": [{"name": "asing_beli", "dir": 1}]}   # pilar struktural -> lolos evidence gate
    pid2 = orch._commit(d2)
    assert pid2 == 99 and len(saved) == 1              # sinyal baru -> dicatat normal


def test_forced_horizon_scales_target():
    """force_horizon=1 (besok): ekspektasi & target diperkecil vs default 3-hari, keyakinan turun."""
    import json as _json
    from app.agents import heuristic as H
    from app.data import premarket
    from app.agents import skill
    import app.model as M
    H._news_sentiment = lambda t: 0
    premarket.global_brief = lambda: {"score": 0.0}
    skill.calibrated_probability = lambda d, p: p
    M.predict_proba = lambda f: 0.75
    feats = {"rsi14": 40, "consec_down": 0, "consec_up": 0, "cum_change_3d": 3,
             "above_sma20": True, "vol_vs_avg": 1.5, "macd_hist": 0.6, "bollinger_pct_b": 0.5,
             "momentum_10d": 10}
    quote = {"ticker": "T", "price": 1000.0, "volume": 5_000_000.0, "features_json": _json.dumps(feats)}
    base = H.decide("T", quote, None, 1e7)
    d1 = H.decide("T", quote, None, 1e7, force_horizon=1)
    assert d1["horizon_days"] == 1, d1["horizon_days"]
    assert abs(d1["expected_pct"]) < abs(base["expected_pct"]), (d1["expected_pct"], base["expected_pct"])
    assert d1["direction"] == base["direction"]      # arah pendek sama, cuma jendela beda


def test_regime_gate_universal():
    """Gate rezim berlaku utk keputusan LLM juga (bukan cuma heuristik): DOWN lemah + risk-on
    kuat -> FLAT; DOWN kuat tetap DOWN (kasus KETR 2026-07-03)."""
    from app.data import premarket, flow
    premarket.global_brief = lambda: {"score": 2.96}
    flow.local_reversal = lambda: (False, "")   # netralkan rezim LOKAL (DB nyata) → uji efek lean GLOBAL saja
    weak = orch._apply_regime_gate({"ticker": "KETR", "direction": "DOWN", "probability": 63.1,
                                    "expected_pct": -0.8, "entry_price": 505, "target_price": 505,
                                    "action": "HOLD", "key_factors": []})
    assert weak["direction"] == "FLAT", weak
    strong = orch._apply_regime_gate({"ticker": "X", "direction": "DOWN", "probability": 70,
                                      "expected_pct": -3, "entry_price": 100, "target_price": 97,
                                      "action": "HOLD", "key_factors": []})
    assert strong["direction"] == "DOWN", strong
    premarket.global_brief = lambda: {"score": 0.0}   # risk-off: DOWN lemah tetap DOWN
    off = orch._apply_regime_gate({"ticker": "Y", "direction": "DOWN", "probability": 60,
                                   "expected_pct": -1, "entry_price": 100, "target_price": 99,
                                   "action": "HOLD", "key_factors": []})
    assert off["direction"] == "DOWN", off


if __name__ == "__main__":
    test_target_hit_closes_early_and_reanalyzes()
    test_same_day_prediction_not_bracket_closed()
    test_bracket_symmetric_below_floor()
    test_target_derived_from_expected()
    test_commit_skips_duplicate_open_prediction()
    test_forced_horizon_scales_target()
    test_regime_gate_universal()
    print("OK test_target_hit: win/loss-awal + dedup + horizon-1 scaling + gate rezim universal.")
