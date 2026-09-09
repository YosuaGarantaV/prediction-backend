"""Cek parser arus asing IDX (python test_idxflow.py) — tanpa jaringan (mock _fetch)."""
import tempfile
from pathlib import Path

from app.data import idxflow

FIX = [
    {"StockCode": "BBCA", "Close": 9000, "ForeignBuy": 1000, "ForeignSell": 600},   # net +400 lbr
    {"StockCode": "GOTO", "Close": 70, "ForeignBuy": 200, "ForeignSell": 900},       # net -700 lbr
]


def test_parse_and_net():
    idxflow._fetch = lambda d: FIX            # mock jaringan
    idxflow.FOREIGN_FILE = Path(tempfile.mkdtemp()) / "f.json"
    data = idxflow.fetch_foreign()
    assert data["BBCA"]["net_vol"] == 400
    assert data["BBCA"]["net_val"] == 400 * 9000          # vol × harga = Rupiah
    assert data["GOTO"]["net_val"] == -700 * 70
    # simpan lalu baca brief
    idxflow.FOREIGN_FILE.write_text(__import__("json").dumps(data), encoding="utf-8")
    assert "NET BELI" in idxflow.foreign_brief("BBCA")
    assert "NET JUAL" in idxflow.foreign_brief("GOTO")
    assert "belum tersedia" in idxflow.foreign_brief("XXXX")
    print("ok")


def test_foreign_pressure():
    import json
    idxflow.FOREIGN_FILE = Path(tempfile.mkdtemp()) / "fp.json"
    idxflow.FOREIGN_FILE.write_text(json.dumps({
        "DUMP": {"fbuy": 100, "fsell": 900, "net_val": 0, "close": 1},   # jual berat
        "BUYD": {"fbuy": 800, "fsell": 200, "net_val": 0, "close": 1},   # beli
        "ZERO": {"fbuy": 0, "fsell": 0, "net_val": 0, "close": 1},       # tanpa transaksi
    }), encoding="utf-8")
    assert idxflow.foreign_pressure("DUMP") == -0.8     # (100-900)/1000
    assert idxflow.foreign_pressure("BUYD") == 0.6
    assert idxflow.foreign_pressure("ZERO") is None     # tak bisa dibagi 0
    assert idxflow.foreign_pressure("XXXX") is None
    assert idxflow.foreign_pressure("DUMP") <= -0.4     # ke-veto BUY
    print("foreign_pressure ok")


if __name__ == "__main__":
    test_parse_and_net()
    test_foreign_pressure()
    print("ALL OK")
