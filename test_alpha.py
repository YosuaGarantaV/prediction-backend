"""Cek uji alpha (repo.alpha_scoreboard): win-rate mentah vs relatif-pasar. Menang mentah saat
pasar juga turun = beta, bukan skill; alpha memisahkan keduanya."""
from app import repo


class _Conn:
    def __init__(self, ihsg, preds):
        self._ihsg = ihsg
        self._preds = preds

    def execute(self, q, *a):
        rows = self._ihsg if "ticker='IHSG'" in q else self._preds
        class C:
            def __init__(s, r): s._r = r
            def __iter__(s): return iter(s._r)   # cursor asli iterable
            def fetchall(s): return s._r
        return C(rows)


def test_beta_vs_alpha_separated():
    # IHSG turun 5.000 -> 4.900 (-2%) dalam window. Saham DOWN yang cuma ikut turun -2% =
    # menang MENTAH tapi TIDAK alpha (excess 0). Saham DOWN yang turun -5% = alpha.
    ihsg = [{"d": "2026-01-01", "close": 5000.0}, {"d": "2026-01-05", "close": 4900.0}]
    preds = [
        {"ts": "2026-01-01T00:00", "resolved_at": "2026-01-05T00:00", "direction": "DOWN", "actual_pct": -2.0},
        {"ts": "2026-01-01T00:00", "resolved_at": "2026-01-05T00:00", "direction": "DOWN", "actual_pct": -2.0},
        {"ts": "2026-01-01T00:00", "resolved_at": "2026-01-05T00:00", "direction": "DOWN", "actual_pct": -5.0},
        {"ts": "2026-01-01T00:00", "resolved_at": "2026-01-05T00:00", "direction": "DOWN", "actual_pct": -5.0},
        {"ts": "2026-01-01T00:00", "resolved_at": "2026-01-05T00:00", "direction": "DOWN", "actual_pct": -2.0},
    ]
    repo.get_conn = lambda: _Conn(ihsg, preds)
    board = repo.alpha_scoreboard()
    d = next(x for x in board if x["direction"] == "DOWN")
    assert d["n"] == 5
    assert d["raw_win_rate"] == 100.0, d          # semua turun >0.5% = menang mentah
    assert d["alpha_win_rate"] == 40.0, d          # hanya 2/5 turun lebih dari pasar (ex<-0.3)
    assert d["avg_excess"] > 0, d                  # rata2 sedikit ungguli (searah DOWN)


if __name__ == "__main__":
    test_beta_vs_alpha_separated()
    print("OK test_alpha: mentah vs alpha terpisah (menang-ikut-pasar bukan skill).")
