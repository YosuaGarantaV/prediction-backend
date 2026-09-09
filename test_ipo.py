"""Cek parser e-IPO (python test_ipo.py) — tanpa jaringan, pakai fixture HTML."""
from app.data import ipo

FIX = """
<div class="pricing-title"><h3>Book Building</h3></div>
<h5 class="nobottommargin">PT Bach Multi Global Tbk.<br>(BACH)<br><span>Syariah</span></h5>
<div class="pricing-title"><h3>Closed</h3></div>
<h5 class="nobottommargin">PT Merdeka Gold Resources Tbk<br>(EMAS)</h5>
"""


def test_parse():
    recs = ipo._parse(FIX)
    assert len(recs) == 2, recs
    d = {r["code"]: r for r in recs}
    assert d["BACH"]["status"] == "Book Building", d["BACH"]
    assert d["BACH"]["company"].startswith("PT Bach Multi"), d["BACH"]
    assert d["EMAS"]["status"] == "Closed", d["EMAS"]   # status terdekat di atasnya
    print("ok")


if __name__ == "__main__":
    test_parse()
