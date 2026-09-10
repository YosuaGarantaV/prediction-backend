"""Cek kalibrasi probability ke data nyata (python test_calib.py)."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from app.agents import skill


def _fake(direction, n, win_rate):
    wins = int(n * win_rate)
    return [{"direction": direction, "probability": 75,
             "outcome": "win" if i < wins else "loss"} for i in range(n)]


def test_calibration(monkeypatch=None):
    # < 8 sampel → pakai formula apa adanya
    skill.repo.resolved_predictions = lambda *_: _fake("UP", 3, 0.9)
    assert skill.calibrated_probability("UP", 80) == 80.0

    # banyak data & menang cuma 30% → keyakinan 80% ditarik TURUN mendekati realisasi
    skill.repo.resolved_predictions = lambda *_: _fake("UP", 80, 0.30)
    out = skill.calibrated_probability("UP", 80)
    assert out < 55, out  # tak boleh tetap overconfident 80%

    # FLAT → tetap apa adanya
    assert skill.calibrated_probability("FLAT", 50) == 50.0
    print("ok")


def test_level_pakai_base_rate_pasar():
    """Level harus diukur dari base rate pasar, bukan 50% (bug vonis 2026-07-29).

    Ambang menang |gerak|>0,5% bikin gerak di dalam band KALAH untuk UP maupun DOWN, jadi
    koin adil mentok ~43% di h=5. Membandingkan ke 50 memvonis mesin "di bawah acak" padahal
    ia di ATAS pasar.
    """
    from app import backtest

    # Campuran UP+DOWN h=5 WAJIB jatuh DI ANTARA kedua base rate itu, dan jelas bukan 50%.
    # Dulu batasnya dipaku 40-45 = angka tabel Juli; refresh base rate yang SAH (2026-08-24
    # menaikkan tiap sel 1,8-3,7 pp) langsung memerahkan test padahal tak ada bug. Diturunkan
    # ke invarian yang sebenarnya diuji, jadi pembaruan tabel tak lagi bikin alarm palsu tapi
    # regresi nyata (mis. diam-diam jatuh ke 50) tetap tertangkap.
    from app.eval import BASELINE_WINRATE as BW
    lo, hi = sorted((BW[('UP', 5)], BW[('DOWN', 5)]))
    base = backtest.market_baseline(5, 5493, 5763)
    assert lo <= base <= hi, (base, lo, hi)
    assert abs(base - 50.0) > 3.0, base

    # OOS 46,6% = DI ATAS pasar → tak boleh lagi divonis "di bawah acak"
    assert "LEMAH" not in backtest._verdict(46.6, -1.9, base)
    # dan yang benar-benar di bawah pasar tetap harus divonis LEMAH
    assert "LEMAH" in backtest._verdict(base - 3.0, -1.9, base)

    # edge positif TAPI belum terbukti per-tanggal → mentok Lv.3, bukan langsung "Menengah"
    lvl, label = skill._level(68, 3.8, 3.8, -1.9, proven=False)
    assert lvl == 3 and "belum signifikan" in label, (lvl, label)
    # terbukti → baru boleh naik
    assert skill._level(68, 3.8, 3.8, -1.9, proven=True)[0] == 4
    # di bawah base rate → tetap Lv.2
    assert skill._level(68, -2.0, -2.0, -1.9, proven=False)[0] == 2
    # tanda overfit menang atas segalanya
    assert skill._level(68, 9.0, 9.0, 15.0, proven=True)[0] == 2

    # base rate live ikut komposisi arah+horizon yang dipertaruhkan, bukan konstanta
    up5 = [{"direction": "UP", "horizon_days": 5}] * 10
    dn5 = [{"direction": "DOWN", "horizon_days": 5}] * 10
    assert skill.market_baseline_live(up5) < skill.market_baseline_live(dn5)
    assert skill.market_baseline_live([]) == 50.0
    print("ok")


if __name__ == "__main__":
    test_calibration()
    test_level_pakai_base_rate_pasar()
