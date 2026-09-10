"""Cek fix root-cause Lv.5→Lv.2 (2026-07-13): (a) local_reversal pakai tren SMA5 (tahan dip),
(b) skill level = keterampilan ARAH saja (FLAT tak menghukum). python test_regime_trend.py."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from app import repo
from app.data import flow
from app.agents import skill


def _fake_ihsg(closes_newest_first):
    """Stub prices IHSG (terbaru dulu) + flow snapshot."""
    class _Conn:
        def execute(self, *a):
            class _C:
                def fetchall(_self):
                    return [{"close": c} for c in closes_newest_first]
            return _C()
    repo.get_conn = lambda: _Conn()


def test_uptrend_survives_single_dip():
    """8 Jul: IHSG dip 5986→5873 mematahkan green-streak, TAPI 5873 > SMA5 → tren utuh.
    local_reversal HARUS tetap ON (menutup celah 28 DOWN yang rugi)."""
    # terbaru dulu: 5873 (dip hari ini), lalu 5986,5916,5876,5745,5695
    _fake_ihsg([5873, 5986, 5916, 5876, 5745, 5695])
    repo.latest_flow = lambda: {"breadth_up": 60, "flow_score": 0.0}
    up, last, sma5 = flow.ihsg_uptrend()
    assert up and last == 5873, (up, last, sma5)     # 5873 > SMA5(~5844) = tren naik utuh
    on, note = flow.local_reversal()
    assert on, note                                   # local reversal ON via SMA5 (streak patah)
    assert flow.ihsg_green_streak() == 0              # konfirmasi: streak MEMANG patah hari ini
    print("uptrend tahan dip: local_reversal ON via SMA5 walau streak patah ok")


def test_genuine_downtrend_allows_down():
    """Downtrend nyata (IHSG di BAWAH SMA5, tak ada streak) → local_reversal OFF → DOWN boleh."""
    _fake_ihsg([5600, 5700, 5800, 5850, 5900, 5950])  # turun terus (terbaru 5600 << SMA5)
    repo.latest_flow = lambda: {"breadth_up": 40, "flow_score": -0.5}
    on, note = flow.local_reversal()
    assert not on, note
    print("downtrend nyata: local_reversal OFF, DOWN tetap boleh ok")


def test_skill_level_excludes_flat():
    """FLAT (non-taruhan, ~16% menang) TIDAK boleh menghukum level skill-arah."""
    from app import backtest
    backtest.load_result = lambda: {"oos_win_rate": 55.0, "overfit_gap": 5,
                                    "horizon": 3, "up_n": 100, "down_n": 0}

    # `resolved_at` disebar ke 25 tanggal: sejak 2026-07-29 level lewat gerbang bukti
    # per-TANGGAL (skill._proven_above), jadi outcome tanpa tanggal tak bisa membuktikan apa
    # pun dan level mentok Lv.3. Yang diuji di sini tetap sama: FLAT tak boleh menyeret level.
    def _rec(i, direction, outcome, prob):
        return {"direction": direction, "outcome": outcome, "probability": prob,
                "horizon_days": 3, "ts": f"2026-07-{1 + i % 25:02d}T02:00:00+00:00",
                "resolved_at": f"2026-07-{1 + i % 25:02d}T09:00:00+00:00"}

    # 100 UP/DOWN @ 60% menang + 100 FLAT @ 10% menang. Level HARUS baca 60%, bukan 35%.
    res = ([_rec(i, "UP", "win" if i < 60 else "loss", 60) for i in range(100)]
           + [_rec(i, "FLAT", "loss", 53) for i in range(100)])
    repo.resolved_predictions = lambda *a, **k: res
    s = skill.agent_skill()
    assert s["resolved"] == 100, s["resolved"]        # cuma UP/DOWN dihitung
    assert s["flat_excluded"] == 100, s["flat_excluded"]
    assert s["calibrated_win_rate"] >= 55, s          # ~60% (bukan 35% tercemar FLAT)
    assert s["level"] >= 5, s                          # 60% arah = Mahir, bukan Lv.2
    print(f"skill level abaikan FLAT: cal={s['calibrated_win_rate']}% level={s['level']} ok")


if __name__ == "__main__":
    test_uptrend_survives_single_dip()
    test_genuine_downtrend_allows_down()
    test_skill_level_excludes_flat()
    print("ALL OK")
