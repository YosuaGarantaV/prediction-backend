"""Cooldown FLAT: saham yang baru saja di-FLAT-kan tak boleh menyita slot LLM tiap siklus.

Menguji logika seleksi saja (tanpa DB/LLM) — persis ekspresi di orchestrator.run_cycle.
pytest tak terpasang di venv live → jalankan langsung: python test_flat_cooldown.py
"""
import time

from app.agents import orchestrator as orc


def _pick(scored, n_llm, aligned=lambda t: True):
    """Salinan seleksi kandidat run_cycle (urutan cek sama: cooldown dulu, baru _aligned)."""
    fresh = time.time() - orc.FLAT_RECHECK_MIN * 60
    return [t for t in scored if orc._flat_last.get(t, 0) < fresh and aligned(t)][:n_llm]


def test_flat_membebaskan_slot_ke_kandidat_berikutnya():
    orc._flat_last.clear()
    scored = ["BMRI", "BBCA", "PGAS", "MEDC", "AKRA", "TINS", "DEWA", "BUMI"]
    assert _pick(scored, 5) == ["BMRI", "BBCA", "PGAS", "MEDC", "AKRA"]

    # 5 teratas baru saja FLAT → slot turun ke peringkat berikutnya, TIDAK hangus.
    for t in scored[:5]:
        orc._flat_last[t] = time.time()
    assert _pick(scored, 5) == ["TINS", "DEWA", "BUMI"]


def test_cooldown_kedaluwarsa_saham_boleh_kembali():
    orc._flat_last.clear()
    orc._flat_last["BMRI"] = time.time() - (orc.FLAT_RECHECK_MIN + 1) * 60
    assert _pick(["BMRI", "TINS"], 5) == ["BMRI", "TINS"]


def test_cooldown_didahulukan_sebelum_aligned():
    """Cek murah dulu: _aligned (memanggil model.predict_proba) tak boleh jalan utk nama
    yang toh dilewati — ini alasan urutan `and` di run_cycle tidak boleh dibalik."""
    orc._flat_last.clear()
    orc._flat_last["BMRI"] = time.time()
    dipanggil = []

    def aligned(t):
        dipanggil.append(t)
        return True

    _pick(["BMRI", "TINS"], 5, aligned)
    assert dipanggil == ["TINS"], dipanggil


def test_flat_skip_menandai_cooldown():
    """Kontrak dengan _commit: cabang flat_skip menulis _flat_last (bukan cuma nge-log)."""
    import inspect
    src = inspect.getsource(orc._commit)
    assert "_flat_last[decision[\"ticker\"]] = time.time()" in src, \
        "cabang FLAT di _commit tak lagi menandai cooldown — seleksi akan berputar lagi"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK  {name}")
    print("semua lolos")
