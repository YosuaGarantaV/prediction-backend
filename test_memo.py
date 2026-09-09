"""Cek memoization tesis analyst (app.agents.memo): reuse saat input SAMA, mikir-ulang saat
ada perubahan nyata (berita/harga/rezim/asing/hari/TTL). Lihat memory saham-idx-prediction."""
from datetime import datetime, timezone, timedelta

from app import repo
from app.agents import memo


def _reset(news_key=(2, 100), lean=1, fp=0.3):
    memo.clear()
    repo.ticker_news_key = lambda t, **k: news_key
    # stub sumber rezim + asing agar deterministik
    import app.data.premarket as pm
    import app.data.idxflow as ix
    pm.global_brief = lambda: {"score": lean}
    ix.foreign_pressure = lambda t: fp


def test_reuse_when_unchanged():
    _reset()
    q = {"price": 1000.0}
    assert memo.get_thesis("AAAA", q) is None            # cold → miss
    memo.put_thesis("AAAA", q, "tesis: UP oversold")
    assert memo.get_thesis("AAAA", q) == "tesis: UP oversold"  # input sama → reuse
    assert memo.stats()["reused"] == 1


def test_price_move_invalidates():
    _reset()
    q = {"price": 1000.0}
    memo.put_thesis("AAAA", q, "tesis")
    assert memo.get_thesis("AAAA", {"price": 1005.0}) == "tesis"      # +0.5% < 1.2% → reuse
    assert memo.get_thesis("AAAA", {"price": 1020.0}) is None         # +2.0% > 1.2% → fresh


def test_new_news_invalidates():
    _reset(news_key=(2, 100))
    q = {"price": 1000.0}
    memo.put_thesis("AAAA", q, "tesis")
    assert memo.get_thesis("AAAA", q) == "tesis"
    repo.ticker_news_key = lambda t, **k: (3, 101)       # berita baru
    assert memo.get_thesis("AAAA", q) is None


def test_regime_flip_invalidates():
    _reset(lean=1)
    q = {"price": 1000.0}
    memo.put_thesis("AAAA", q, "tesis")
    import app.data.premarket as pm
    pm.global_brief = lambda: {"score": -2}              # risk-on → risk-off
    assert memo.get_thesis("AAAA", q) is None


def test_foreign_flow_invalidates():
    _reset(fp=0.3)
    q = {"price": 1000.0}
    memo.put_thesis("AAAA", q, "tesis")
    import app.data.idxflow as ix
    ix.foreign_pressure = lambda t: -0.5                 # asing balik jual
    assert memo.get_thesis("AAAA", q) is None


def test_ttl_invalidates():
    _reset()
    q = {"price": 1000.0}
    memo.put_thesis("AAAA", q, "tesis")
    # mundurkan ts melewati TTL
    memo._CACHE["AAAA"]["ts"] = datetime.now(timezone.utc) - timedelta(hours=memo.TTL_HOURS + 0.5)
    assert memo.get_thesis("AAAA", q) is None


if __name__ == "__main__":
    for fn in [test_reuse_when_unchanged, test_price_move_invalidates, test_new_news_invalidates,
               test_regime_flip_invalidates, test_foreign_flow_invalidates, test_ttl_invalidates]:
        fn()
        print("OK", fn.__name__)
    print("OK test_memo: reuse saat sama; mikir-ulang saat berita/harga/rezim/asing/TTL berubah.")
