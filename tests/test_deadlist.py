"""Cek logika daftar-mati universe (jalankan: python test_deadlist.py)."""
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import tempfile
from datetime import date
from pathlib import Path

from app import universe


def test_dead_cycle():
    universe.DEAD_FILE = Path(tempfile.mkdtemp()) / "dead.json"

    # < threshold → belum dianggap mati
    universe.update_dead({"XXXX"}, set())
    assert "XXXX" not in universe.dead_set(), "1 miss tak boleh langsung mati"

    # capai threshold (3) → mati, di-skip
    universe.update_dead({"XXXX"}, set())
    universe.update_dead({"XXXX"}, set())
    assert "XXXX" in universe.dead_set(), "3 miss harus dianggap mati"

    # muncul lagi (ada data) → dihapus dari daftar-mati
    universe.update_dead(set(), {"XXXX"})
    assert "XXXX" not in universe.dead_set(), "ticker hidup lagi harus keluar daftar-mati"

    # entri mati yang basi (> recheck window) → di-retry (tak lagi di dead_set)
    stale = date.today().toordinal() - (universe.DEAD_RECHECK_DAYS + 1)
    universe._save_dead({"OLDX": {"misses": 9, "day": stale}})
    assert "OLDX" not in universe.dead_set(), "entri basi harus di-retry"
    print("ok")


def test_harvest_filter():
    # add_discovered membuang yang sudah ada & non-kode
    universe.DISCOVERED_FILE = Path(tempfile.mkdtemp()) / "disc.txt"
    universe.DISCOVERED_AT_FILE = Path(tempfile.mkdtemp()) / "disc_at.json"
    assert universe.add_discovered(["WBSA"]) == 0, "WBSA sudah di RECENT_IPOS"
    assert universe.add_discovered(["ZZZZ"]) == 1, "kode baru harus masuk"
    assert universe.add_discovered(["ZZZZ"]) == 0, "duplikat tak ditambah dua kali"
    print("ok")


def test_ipo_grace_period():
    """IPO baru terdeteksi yang gagal fetch (belum listing) TIDAK boleh masuk dead_set
    selama masih dalam IPO_GRACE_DAYS — supaya begitu listing (kapan pun dlm masa itu)
    langsung otomatis kepakai, bukan nunggu DEAD_RECHECK_DAYS."""
    universe.DEAD_FILE = Path(tempfile.mkdtemp()) / "dead.json"
    universe.DISCOVERED_FILE = Path(tempfile.mkdtemp()) / "disc.txt"
    universe.DISCOVERED_AT_FILE = Path(tempfile.mkdtemp()) / "disc_at.json"

    assert universe.add_discovered(["NEWX"]) == 1
    # 3x gagal fetch berturut (wajar: belum listing) → TETAP tak boleh di-quarantine
    universe.update_dead({"NEWX"}, set())
    universe.update_dead({"NEWX"}, set())
    universe.update_dead({"NEWX"}, set())
    assert "NEWX" not in universe.dead_set(), "IPO dlm grace period tak boleh masuk dead_set"

    # ticker SEED biasa (bukan IPO baru) dgn miss sama → tetap masuk dead_set spt biasa
    universe.update_dead({"OLDY"}, set())
    universe.update_dead({"OLDY"}, set())
    universe.update_dead({"OLDY"}, set())
    assert "OLDY" in universe.dead_set(), "ticker non-IPO tetap kena aturan dead-list normal"

    # discovered TAPI di luar grace window (>30 hari lalu) → kembali kena aturan normal
    at = universe._load_discovered_at()
    at["NEWX"] = date.today().toordinal() - (universe.IPO_GRACE_DAYS + 1)
    universe._save_discovered_at(at)
    assert "NEWX" in universe.dead_set(), "lewat grace period harus kembali ikut aturan dead-list normal"
    print("ok")


if __name__ == "__main__":
    test_dead_cycle()
    test_harvest_filter()
    test_ipo_grace_period()
