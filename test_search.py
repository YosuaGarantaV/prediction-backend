"""Cek search engine berita (python test_search.py) — DB in-memory, FTS5 + fallback LIKE.
Kontrak: relevansi bm25, anti-injeksi FTS5 (AND/OR/sintaks), age filter, LIKE saat FTS mati."""
import sqlite3

from app import db, repo

_ROWS = [
    (1, "2026-07-01", "Harga Nikel Dunia Melejit Usai Pajak Ekspor", "nikel ekspor", 2),
    (2, "2026-07-02", "BI Kerek Suku Bunga Acuan ke 5,75 Persen", "", 1),
    (3, "2026-07-03", "Bank BCA Bagi Dividen Tiga Kali Setahun", "BBCA", 1),
    (4, "2026-01-01", "Berita Lama Soal Nikel Tahun Baru", "nikel", 0),   # untuk age filter
]


def _mem(fts=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE news(id INTEGER PRIMARY KEY, ts TEXT, scope TEXT, source TEXT, "
                 "title TEXT, url TEXT, summary TEXT, tickers TEXT, sentiment TEXT, impact INTEGER)")
    for i, ts, title, tk, imp in _ROWS:
        conn.execute("INSERT INTO news(id,ts,title,url,summary,tickers,impact) VALUES (?,?,?,?,?,?,?)",
                     (i, ts, title, f"u{i}", title, tk, imp))
    if fts:
        conn.executescript(db.FTS_SCHEMA)
        conn.execute("INSERT INTO news_fts(news_fts) VALUES('rebuild')")
    conn.commit()
    repo.get_conn = lambda: conn
    db.FTS_ENABLED = fts
    return conn


def test_relevance_fts():
    _mem(fts=True)
    rows = repo.search_news("nikel ekspor", limit=5)
    assert rows and "Nikel" in rows[0]["title"], rows
    assert all("nikel" in r["title"].lower() or "ekspor" in r["title"].lower() for r in rows), rows
    print("relevansi FTS bm25 ok")


def test_injection_safe():
    _mem(fts=True)
    for bad in ["nikel AND OR (", 'BI: bunga?', "*", '"" NEAR', ""]:
        repo.search_news(bad)   # tak boleh melempar
    assert repo.search_news("nikel AND OR (")  # AND/OR jadi literal → tetap dapat 'nikel'
    print("anti-injeksi FTS5 ok (keyword & sintaks jadi literal)")


def test_age_filter():
    _mem(fts=True)
    recent = repo.search_news("nikel", max_age_days=90)   # buang berita 2026-01
    ids = {r["id"] for r in recent}
    assert 1 in ids and 4 not in ids, ids
    print("age filter ok (berita lama tersaring)")


def test_like_fallback():
    _mem(fts=False)   # simulasikan build tanpa FTS5
    rows = repo.search_news("dividen", limit=5)
    assert rows and any("Dividen" in r["title"] for r in rows), rows
    print("fallback LIKE ok (tanpa FTS5)")


def test_empty_returns_nothing():
    _mem(fts=True)
    assert repo.search_news("") == [] and repo.search_news("a x") == []
    print("query kosong/terlalu-pendek -> [] ok")


if __name__ == "__main__":
    test_relevance_fts()
    test_injection_safe()
    test_age_filter()
    test_like_fallback()
    test_empty_returns_nothing()
    print("ALL OK")
