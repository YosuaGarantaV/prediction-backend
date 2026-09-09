"""DEV: validasi sumber YouTube + parse teks (judul/deskripsi, transcript opsional).

Pakai pipeline berita yang sudah ada (app.data.news) untuk menilai sentimen &
apakah berita itu market-wide (penggerak IHSG). Tidak menyentuh DB / engine.

Contoh:
  # resolve @handle -> channel_id + validasi RSS (untuk ditaruh di config.NEWS_YOUTUBE)
  python tools/yt_news.py resolve @CNBCIndonesia @SekretariatPresiden @KOMPASTV

  # tarik video terbaru dari channel & nilai judul/ringkasannya
  python tools/yt_news.py feed UCxZIDJamFPFcROBylMcwHWg

  # parse transcript 1 video (butuh: pip install youtube-transcript-api)
  python tools/yt_news.py transcript "https://www.youtube.com/watch?v=XXXX"

ponytail: produksi pakai RSS (feedparser sudah ada). Transcript opsional & cuma di dev.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.data import news  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


def resolve(handle: str) -> str | None:
    """@handle / URL channel -> channelId (UC...). None kalau gagal."""
    h = handle.strip().lstrip("@")
    url = h if h.startswith("http") else f"https://www.youtube.com/@{h}"
    try:
        html = requests.get(url, headers=UA, timeout=15).text
    except Exception as e:  # noqa: BLE001
        print(f"  ! gagal fetch {url}: {e}")
        return None
    m = (re.search(r'"channelId":"(UC[\w-]{22})"', html)
         or re.search(r'channel/(UC[\w-]{22})', html))
    return m.group(1) if m else None


def _verdict(blob: str) -> str:
    sent, impact = news._sentiment(blob)
    wide = news._is_market_wide(blob)
    tags = news._tag_tickers(blob)
    return (f"sentimen={sent} impact={impact:+d} "
            f"{'[MARKET-WIDE]' if wide else '[lokal]'} "
            f"tickers={tags or '-'}")


def cmd_resolve(handles: list[str]) -> None:
    print("# tempel baris valid ini ke config.NEWS_YOUTUBE:")
    for h in handles:
        cid = resolve(h)
        if not cid:
            print(f'  # {h}: TIDAK ketemu channelId')
            continue
        feed = news._fetch_feed(RSS.format(cid), "local")
        ok = "OK" if feed else "feed KOSONG (cek lagi)"
        print(f'  "{RSS.format(cid)}",  # {h} -> {len(feed)} item ({ok})')


def cmd_feed(cid: str) -> None:
    items = news._fetch_feed(RSS.format(cid), "local")
    print(f"{len(items)} video relevan (sudah lolos filter berita):")
    for it in items[:15]:
        print(f"- {it['title']}")
        print(f"    {_verdict(it['title'] + ' ' + it['summary'])}")


def _video_id(s: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", s)
    return m.group(1) if m else s


def cmd_transcript(url: str) -> None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        print("transcript butuh: pip install youtube-transcript-api  (opsional, dev only)")
        return
    vid = _video_id(url)
    try:
        parts = YouTubeTranscriptApi.get_transcript(vid, languages=["id", "en"])
    except Exception as e:  # noqa: BLE001
        print(f"gagal ambil transcript {vid}: {e}")
        return
    text = " ".join(p["text"] for p in parts)
    print(f"transcript {vid}: {len(text)} char")
    print(f"VERDICT (full): {_verdict(text)}")
    # juga nilai per-kalimat penting (cari kalimat paling negatif/market-wide)
    for sent in re.split(r"(?<=[.!?])\s+", text):
        s, imp = news._sentiment(sent)
        if abs(imp) >= 2 or (news._is_market_wide(sent) and s != "neutral"):
            print(f"  • [{s} {imp:+d}] {sent.strip()[:140]}")


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "resolve":
        cmd_resolve(args)
    elif cmd == "feed":
        cmd_feed(args[0])
    elif cmd == "transcript":
        cmd_transcript(args[0])
    else:
        print(f"perintah tak dikenal: {cmd}\n{__doc__}")


if __name__ == "__main__":
    main()
