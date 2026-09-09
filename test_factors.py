"""Cek faktor struktural bernama (app.agents.factors) + papan skor evaluasi (repo.factor_scoreboard).
Faktor = arus asing + aksi korporasi dari berita ter-tag; dicatat per-prediksi utk evaluasi harian."""
import json

from app import repo
from app.agents import factors


def test_signals_fire_by_type():
    # arus asing kuat + aksi korporasi dari judul → faktor bernama
    import app.data.idxflow as ix
    ix.foreign_pressure = lambda t: -0.6                       # asing jual kuat
    repo.news_for_ticker = lambda *a, **k: [
        {"title": "Jadwal Pembagian Dividen ABCD Tahun Buku 2025"},
        {"title": "ABCD Umumkan Rights Issue HMETD I"},
    ]
    sigs = factors.extra_signals("ABCD", {}, 1000)
    names = {s["name"]: s["dir"] for s in sigs}
    assert names.get("asing_jual") == -1, names
    assert names.get("ca_dividen") == +1, names
    assert names.get("ca_rights_issue") == -1, names


def test_apply_adjusts_score_and_records():
    import app.data.idxflow as ix
    ix.foreign_pressure = lambda t: 0.5                        # asing beli
    repo.news_for_ticker = lambda *a, **k: []
    factors.muted_factors = lambda: {}                         # hermetis: tanpa mute dari DB nyata
    fac = []
    score, fac, sigs = factors.apply("ABCD", {}, 1000, 0.0, fac)
    assert score > 0, score                                    # asing beli menaikkan skor
    assert any(s["name"] == "asing_beli" for s in sigs)
    assert any("asing_beli" in f for f in fac)


def test_apply_mutes_proven_loser_but_still_records():
    """Faktor ber-win-rate jauh di bawah base DINOLKAN bobotnya, tapi TETAP tercatat di
    signals (scoreboard terus mengumpulkan data → bisa pulih sendiri)."""
    import app.data.idxflow as ix
    ix.foreign_pressure = lambda t: 0.5                        # asing beli fired
    repo.news_for_ticker = lambda *a, **k: []
    factors.muted_factors = lambda: {"asing_beli": "36/104"}
    fac = []
    score, fac, sigs = factors.apply("ABCD", {}, 1000, 0.0, fac)
    assert score == 0.0, score                                 # bobot dinolkan
    assert any(s["name"] == "asing_beli" for s in sigs)        # tetap tercatat utk evaluasi
    assert any("DIBISUKAN" in f for f in fac), fac


def test_scoreboard_aggregates(monkeypatch=None):
    # dua prediksi resolved dgn faktor tersimpan → win-rate per-faktor
    # ts/resolved_at wajib: papan skor kini memakai penjaga has_new_session (definisi resolusi
    # sah tunggal), bukan lagi perbandingan tanggal WIB di SQL. Jarak 7 hari = pasti lewat sesi.
    _ts = "2026-07-20T02:00:00+00:00"
    _res = "2026-07-27T09:30:00+00:00"
    fake = [
        {"direction": "DOWN", "outcome": "win", "ts": _ts, "resolved_at": _res,
         "factors_json": json.dumps({"signals": [{"name": "asing_jual", "dir": -1}]})},
        {"direction": "DOWN", "outcome": "loss", "ts": _ts, "resolved_at": _res,
         "factors_json": json.dumps({"signals": [{"name": "asing_jual", "dir": -1}]})},
        {"direction": "DOWN", "outcome": "win", "ts": _ts, "resolved_at": _res,
         "factors_json": json.dumps({"signals": [{"name": "asing_jual", "dir": -1}]})},
    ]

    class _Conn:
        def execute(self, *a):
            class C:
                def fetchall(self_):
                    return fake
            return C()
    repo.get_conn = lambda: _Conn()
    board = repo.factor_scoreboard(min_n=3)
    row = next(r for r in board if r["factor"] == "asing_jual")
    assert row["n"] == 3 and row["win"] == 2 and row["win_rate"] == 66.7, row


if __name__ == "__main__":
    test_signals_fire_by_type()
    test_apply_adjusts_score_and_records()
    test_apply_mutes_proven_loser_but_still_records()
    test_scoreboard_aggregates()
    print("OK test_factors: faktor fired per-jenis, apply nudge skor + catat, "
          "mute faktor merugikan, papan skor agregasi.")
