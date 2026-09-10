"""Base rate harus diukur di jendela yang SAMA dengan lengan yang dinilai (audit 2026-09-08).

Gerbang uang membuka 7-8 Sep 2026 karena `arm_verdict` membandingkan win-rate lengan UP
(50,7%, resolusi bracket, median hold 0,94 hari) ke tabel statis 41,0% yang diukur atas return
horizon-tetap sepanjang seluruh riwayat. Selisih 9,7 pp itu perbedaan POPULASI, bukan edge.
Test ini gagal kalau tabel statis kembali menyetir vonis lengan.

pytest tak terpasang di venv live → jalankan langsung: python test_baserate_matched.py
"""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
from app import eval as ev
from app import repo
from app.agents import skill


def _rows(n_up: int, n_down: int, n_flatmove: int) -> list[dict]:
    """Baris resolved sintetis: `actual_pct` menentukan base rate, `outcome` win/loss lengan."""
    out = []
    for i in range(n_up):
        out.append({"direction": "UP", "horizon_days": 3, "actual_pct": 2.0,
                    "outcome": "win", "ts": f"2026-08-{i % 25 + 1:02d}",
                    "resolved_at": f"2026-08-{i % 25 + 1:02d}T09:00:00+00:00"})
    for i in range(n_down):
        out.append({"direction": "DOWN", "horizon_days": 3, "actual_pct": -2.0,
                    "outcome": "loss", "ts": f"2026-08-{i % 25 + 1:02d}",
                    "resolved_at": f"2026-08-{i % 25 + 1:02d}T09:00:00+00:00"})
    for i in range(n_flatmove):
        out.append({"direction": "UP", "horizon_days": 3, "actual_pct": 0.1,
                    "outcome": "loss", "ts": f"2026-08-{i % 25 + 1:02d}",
                    "resolved_at": f"2026-08-{i % 25 + 1:02d}T09:00:00+00:00"})
    return out


def test_measured_baseline_menghitung_tebakan_konstan():
    """Base rate = skor strategi 'selalu bilang X' pada baris yang sama."""
    rows = _rows(50, 30, 20)          # 100 baris: 50 naik >0,5%, 30 turun <-0,5%, 20 diam
    assert ev.measured_baseline("UP", rows) == 50.0
    assert ev.measured_baseline("DOWN", rows) == 30.0
    assert ev.measured_baseline("FLAT", rows) is None


def test_sampel_tipis_jatuh_ke_tabel():
    """Di bawah min_n angkanya tak bisa dipercaya → None, dan pemanggil memakai tabel."""
    assert ev.measured_baseline("UP", _rows(5, 5, 5)) is None
    assert ev.measured_baseline("UP", _rows(30, 20, 10), min_n=40) == 50.0


def test_win_band_satu_definisi():
    """eval.WIN_BAND_PCT dan orchestrator.is_win harus memakai mistar yang sama."""
    from app.agents import orchestrator as orc
    b = ev.WIN_BAND_PCT
    assert orc.is_win("UP", b + 0.01) and not orc.is_win("UP", b - 0.01)
    assert orc.is_win("DOWN", -b - 0.01) and not orc.is_win("DOWN", -b + 0.01)


def test_arm_verdict_memakai_base_rate_terukur():
    """Lengan yang cuma menyamai tebakan konstan TIDAK boleh lolos side='above'.

    Ini kasus produksi 8 Sep: UP menang 50,7% sementara 50,2% baris di jendela yang sama
    memang bergerak >+0,5%. Dengan tabel 41,0 vonisnya LOLOS; dengan base rate terukur GAGAL.
    """
    # 150 baris UP, setengahnya menang; 150 baris pembanding dgn proporsi gerak yang sama.
    rows = []
    for i in range(150):
        naik = i % 2 == 0
        rows.append({"direction": "UP", "horizon_days": 3,
                     "actual_pct": 2.0 if naik else -1.0,
                     "outcome": "win" if naik else "loss",
                     "ts": f"2026-08-{i % 25 + 1:02d}",
                     "resolved_at": f"2026-08-{i % 25 + 1:02d}T09:00:00+00:00"})
    repo.resolved_predictions = lambda n=500: rows
    skill._ARM_CACHE.clear()
    v = skill.arm_verdict("UP", 2, side="above")
    assert v["baseline_source"] == "diukur", v
    assert abs(v["baseline"] - 50.0) < 0.1, v          # bukan 41,0 dari tabel
    assert not v["pass"], v                            # 50% vs base 50% = tak ada edge

    # Kontrol: lengan yang benar-benar unggul tetap lolos, jadi gerbang tak sekadar mati.
    for r in rows[:120]:
        r["outcome"] = "win"
    skill._ARM_CACHE.clear()
    assert skill.arm_verdict("UP", 2, side="above")["pass"]


def test_listing_baru_ditolak_scanner():
    """JELI: dibeli 6x pada 17 Jul saat usianya 9 sesi, menyumbang 38% seluruh kerugian.
    Syarat lama `n >= 5` cuma menghitung sesi di dalam jendela 30 hari, jadi listing bayi
    tetap lolos. Saham ramai yang riwayatnya panjang harus tetap lolos."""
    import sqlite3

    import config
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE prices (ticker TEXT, ts TEXT, close REAL, volume REAL)")
    for i in range(60):                      # ticker tua & ramai
        conn.execute("INSERT INTO prices VALUES ('TUA', date('now', ?), 1000, 5000000)",
                     (f"-{i} day",))
    for i in range(9):                       # listing bayi, ramai tapi baru 9 sesi
        conn.execute("INSERT INTO prices VALUES ('BAYI', date('now', ?), 1000, 5000000)",
                     (f"-{i} day",))
    conn.commit()
    repo.get_conn = lambda: conn
    assert config.MIN_LIQUID_HISTORY >= 30
    assert repo.liquid_tickers() == {"TUA"}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name} ok")
