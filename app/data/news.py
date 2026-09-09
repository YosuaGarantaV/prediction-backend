"""Ambil berita lokal (RSS media keuangan ID) + global, tagging ticker & sentimen ringkas."""
from __future__ import annotations

import calendar
import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser
import requests

import config
from app import repo, universe

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SahamBot/1.0)"}

NEWS_WORKERS = 16  # I/O-bound (RSS) → naikkan paralelisme, 58 sumber selesai jauh lebih cepat

# Throttle fetch berita per-saham (jangan tarik Google News berulang utk ticker sama)
_ticker_news_at: dict[str, float] = {}
TICKER_NEWS_TTL = 2700  # 45 menit (lebih segar saat jam bursa; dulu 2 jam)


def gnews_url(query: str, fresh_hours: int = 0) -> str:
    """Bangun URL Google News RSS (bhs Indonesia). fresh_hours>0 → batasi berita TERBARU saja."""
    if fresh_hours:
        query = f"{query} when:{fresh_hours}h"
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=id&gl=ID&ceid=ID:id"


def yahoo_ticker_url(ticker: str) -> str:
    """RSS berita per-saham dari Yahoo Finance — sumber CEPAT & spesifik emiten (tanpa Cloudflare)."""
    return (f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}.JK"
            "&region=US&lang=en-US")


def _entry_time(entry) -> str:
    """Waktu terbit asli dari feed (UTC ISO); fallback ke sekarang kalau tak ada."""
    for k in ("published_parsed", "updated_parsed"):
        tm = entry.get(k)
        if tm:
            try:
                return datetime.fromtimestamp(calendar.timegm(tm), tz=timezone.utc).isoformat()
            except Exception:  # noqa: BLE001
                pass
    return datetime.now(timezone.utc).isoformat()

# Nama perusahaan -> ticker untuk tagging berita.
COMPANY_NAMES = {
    "BBCA": ["bca", "bank central asia"],
    "BBRI": ["bri", "bank rakyat"],
    "BMRI": ["mandiri", "bank mandiri"],
    "BBNI": ["bni", "bank negara"],
    "BRIS": ["bsi", "syariah indonesia"],
    "TLKM": ["telkom", "telkomsel"],
    "EXCL": ["xl axiata", "xlsmart", "xl "],
    "ISAT": ["indosat", "ioh"],
    "GOTO": ["gojek", "tokopedia", "gotO"],
    "BUKA": ["bukalapak"],
    "UNVR": ["unilever"],
    "ICBP": ["indofood cbp", "indomie"],
    "INDF": ["indofood"],
    "MYOR": ["mayora"],
    "AMRT": ["alfamart", "sumber alfaria"],
    "GGRM": ["gudang garam"],
    "HMSP": ["sampoerna", "hm sampoerna"],
    "ADRO": ["adaro"],
    "PTBA": ["bukit asam"],
    "ITMG": ["indo tambangraya", "banpu"],
    "INDY": ["indika"],
    "MEDC": ["medco"],
    "PGAS": ["pgn", "perusahaan gas"],
    "AKRA": ["akr corporindo", "akr "],
    "ANTM": ["antam", "aneka tambang"],
    "INCO": ["vale", "inco"],
    "TINS": ["timah"],
    "MDKA": ["merdeka copper", "merdeka gold"],
    "NCKL": ["trimegah bangun", "harita nickel"],
    "ASII": ["astra"],
    "UNTR": ["united tractors"],
    "SMGR": ["semen indonesia", "semen gresik"],
    "INTP": ["indocement"],
    "KLBF": ["kalbe"],
    "CPIN": ["charoen pokphand"],
    "JPFA": ["japfa"],
    "BRPT": ["barito pacific"],
    "TPIA": ["chandra asri"],
    "BREN": ["barito renewables"],
    "ARTO": ["bank jago", "jago"],
    "EMTK": ["emtek", "elang mahkota"],
}

POSITIVE_WORDS = [
    "naik", "menguat", "melonjak", "rekor", "untung", "laba", "tumbuh", "positif",
    "rally", "surge", "gain", "beat", "dividen", "ekspansi", "akuisisi", "optimis",
    "rebound", "cuan", "stimulus", "pemangkasan suku bunga", "rate cut",
]
NEGATIVE_WORDS = [
    "turun", "anjlok", "melemah", "rugi", "merosot", "koreksi", "jeblok", "negatif",
    "drop", "plunge", "crash", "phk", "default", "gagal bayar", "resesi", "krisis",
    "tarif", "tariff", "perang", "war", "inflasi", "sanksi", "boikot", "demo",
    # politik/kebijakan yg bikin investor gugup (sumber guncangan IHSG mendadak)
    "kerusuhan", "mogok", "reshuffle", "pemakzulan", "kudeta", "ketidakpastian",
]
# Peristiwa BESAR penggerak pasar → impact dipaksa ke magnitudo 3 (jarang tapi krusial).
HIGH_IMPACT = [
    "hormuz", "selat hormuz", "blokade", "embargo", "invasi", "serangan", "attack",
    "konflik", "perang", "war", "resesi", "recession", "krisis", "default", "gagal bayar",
    "pemadaman", "blackout", "opec", "shutdown", "chokepoint", "geopolitik",
    "kerusuhan", "kudeta", "pemakzulan", "mogok nasional", "darurat militer",
]
# Hanya term sangat kuat ini yang boleh memaksa magnitudo 3 SENDIRIAN; sisanya butuh ≥2 hit
# (cegah clickbait 1-kata "perang harga"/"saham krisis" memanipulasi sentimen).
STRONG_CRISIS = [
    "invasi", "embargo", "blokade", "gagal bayar", "default", "resesi", "recession",
    "selat hormuz", "perang dunia", "kudeta", "pemakzulan", "darurat militer",
]
# Frasa KONTEKS — dievaluasi lebih dulu (bobot 2): mengoreksi kata tunggal yang menyesatkan,
# mis. "inflasi TURUN" sebenarnya BULLISH walau mengandung kata negatif "turun".
POS_PHRASES = [
    "inflasi turun", "inflasi melandai", "inflasi terkendali", "rupiah menguat",
    "suku bunga turun", "pangkas suku bunga", "pemangkasan suku bunga", "rate cut",
    "the fed pangkas", "bi rate turun", "surplus neraca", "ekonomi tumbuh",
    "laba naik", "laba melonjak", "kinerja positif", "harga minyak turun",
]
NEG_PHRASES = [
    "inflasi naik", "inflasi melonjak", "rupiah melemah", "rupiah anjlok", "rupiah tertekan",
    "suku bunga naik", "kenaikan suku bunga", "rate hike", "the fed tahan",
    "defisit melebar", "ekonomi melambat", "laba turun", "laba anjlok",
    "gelombang phk", "gagal bayar", "harga minyak melonjak",
    # KEBIJAKAN/POLITIK yg memicu jual mendadak (kasus IHSG -3% saat pidato Prabowo soal
    # gaji guru/kopdes/MBG): beban fiskal & ketidakpastian = risk-off di mata investor.
    "defisit anggaran", "defisit apbn", "anggaran membengkak", "beban fiskal",
    "ppn naik", "kenaikan pajak", "pajak naik", "subsidi membengkak",
    "utang pemerintah naik", "reshuffle kabinet", "ketidakpastian politik",
    "demo besar", "mogok nasional", "downgrade rating", "rating diturunkan",
    # INFLASI PANGAN/BAHAN POKOK: kata "naik" polos ter-skor POSITIF (audit 2026-07-13:
    # "Harga Pangan Naik di Semarang, Timun Jadi Komoditas Termahal" salah dpt +2) — harga
    # pangan naik = sinyal inflasi/tekanan daya beli, BUKAN bullish saham.
    "harga pangan naik", "harga pangan melonjak", "harga bahan pokok naik",
    "harga bahan pokok melonjak", "harga sembako naik", "harga sembako melonjak",
]
# Frasa PEREDA — krisis DISEBUT tapi konteksnya justru aman/selesai. Kasus nyata 2026-07-10:
# "Tanker Pertamina BERHASIL Lintasi Selat Hormuz, Pasokan Energi TERJAGA" ter-skor -3
# (darurat palsu, bisa memicu siklus emergency). Ada frasa ini → JANGAN paksa magnitudo 3;
# sentimen dasar kata tetap dihitung (hanya menahan eskalasi, tak membalik arah).
ALL_CLEAR = [
    "berhasil lintasi", "berhasil melintasi", "berhasil tembus", "terjaga", "mereda",
    "aman terkendali", "kembali normal", "gencatan senjata", "deeskalasi", "de-eskalasi",
    "dicabut", "teratasi", "perang berakhir", "konflik berakhir",
]
# Headline promosi/rekomendasi/ramalan = BUKAN info pasar objektif (rawan manipulasi) → dibuang.
NOISE_PATTERNS = [
    "rekomendasi saham", "saham pilihan", "saham hari ini", "raih cuan", "auto ara",
    "ngegas", "ramalan", "zodiak", "togel", "rumus", "jangan lewatkan", "buruan",
    "dijamin", "pasti untung", "pasti cuan", "bocoran", "trading plan", "saham gocap",
    "prediksi skor", "live streaming", "promo",
    "rumah duka", "melayat",  # seremoni/duka = bukan penggerak pasar (lolos via kata 'bisnis')
]

# Berita berdampak PASAR-LUAS (bukan 1 emiten) → dipaksa scope=global agar masuk konteks
# SEMUA analisis saham. Tanpa ini, berita MSCI/BI-rate/rupiah yang ditangkap feed LOKAL
# tersimpan scope=local tanpa tag ticker → tak pernah sampai ke analis (bug yang ditemukan).
# CATATAN: bare "rupiah" DIBUANG dari sini (audit 2026-07-13) — kata itu muncul di HAMPIR
# SEMUA berita berharga-Rupiah ("200 ribu rupiah/bulan") sbg SATUAN MATA UANG, bukan sinyal
# kurs; dulu meloloskan berita non-saham (tarif TransJakarta) jadi "global". Ganti frasa
# spesifik pergerakan kurs (sudah dipakai di NEG/POS_PHRASES) + "nilai tukar"/"kurs" murni.
MARKET_WIDE = (
    "msci", "ihsg", "bi rate", "suku bunga", "bank indonesia", "nilai tukar", "kurs rupiah",
    "rupiah menguat", "rupiah melemah", "rupiah anjlok", "rupiah tertekan", "the fed",
    "federal reserve", "fomc", "inflasi", "resesi", "tarif impor", "opec", "harga minyak",
    "batu bara", "harga nikel", "harga emas", "investor asing", "net sell", "net buy",
    "bursa efek indonesia", "emerging market", "frontier market", "dana asing", "ihsg hari ini",
    "apbn", "defisit anggaran", "subsidi", "danantara", "kabinet", "reshuffle",
    "gaji guru", "kopdes", "koperasi desa merah putih", "makan bergizi gratis", "mbg",
    "kenaikan pajak", "ppn", "kebijakan fiskal", "stimulus fiskal", "utang pemerintah",
)
# TOKOH (presiden/pejabat) yang MENGGERAKKAN pasar HANYA saat disandingkan isu kebijakan/
# ekonomi nyata — nama tokoh SENDIRIAN (kunjungan pribadi/gosip keluarga/seremoni) BUKAN
# sinyal pasar. Kasus nyata yang harus dibuang (audit 2026-07-13): "Anak Hashim Keponakan
# Prabowo Datang ke IKN, Beri Respons Tak Terduga" — menyebut Prabowo tanpa substansi apa pun.
POLICY_FIGURES = ("prabowo", "presiden prabowo", "trump", "powell", "sri mulyani", "menteri keuangan")
POLICY_CONTEXT = (
    "kebijakan", "ekonomi", "anggaran", "apbn", "subsidi", "pajak", "pidato", "kabinet",
    "reshuffle", "investasi", "fiskal", "moneter", "suku bunga", "tarif", "dagang",
    "stimulus", "utang", "danantara", "gaji guru", "kopdes", "koperasi desa", "mbg",
)


def _policy_figure_hit(t: str) -> bool:
    """Nama tokoh + konteks kebijakan/ekonomi BERSAMA (bukan nama sendirian) = sinyal pasar."""
    return any(f in t for f in POLICY_FIGURES) and any(c in t for c in POLICY_CONTEXT)


def _is_market_wide(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in MARKET_WIDE) or _policy_figure_hit(t)


# Istilah PASAR/EKONOMI (Indonesia-sentris) — berita yang tak menyentuh salah satu ini,
# tak menyebut emiten, & impact-nya lemah = bukan penggerak saham (olahraga/hiburan/gaya
# hidup/kriminal/seremoni) → dibuang. Banyak yg bhs Indonesia → otomatis menyaring berita
# perusahaan ASING (B&M, Cathay) yg tak relevan ke IDX.
RELEVANT_TERMS = MARKET_WIDE + (
    "saham", "emiten", "bursa efek", "bei ", "dividen", "buyback", "rights issue",
    "right issue", "obligasi", "ojk", " lps ", "laba bersih", "rugi bersih", "akuisisi",
    "merger", "private placement", "tender offer", "stock split", "kinerja keuangan",
    "pendapatan perusahaan", "apbn", "sbn", "ekspor", "impor", "investasi", "pasar modal",
    "indeks saham", "kurs", "valuta asing", "komoditas", "rights", "right issue", "kredit",
    "perbankan", "neraca dagang", "neraca perdagangan", "pertumbuhan ekonomi", "pmi manufaktur",
)


def _is_relevant(blob: str, tickers: list, impact: int) -> bool:
    """True kalau berita berpeluang menggerakkan saham IDX. Off-topic (olahraga/hiburan/
    gaya hidup/kriminal) → False. Emiten ter-tag ATAU impact kuat ATAU singgung term pasar."""
    if tickers:               # menyebut emiten IDX
        return True
    if abs(impact) >= 2:      # krisis/geopolitik kuat → risk-off lewat minyak/risiko
        return True
    t = blob.lower()
    return any(k in t for k in RELEVANT_TERMS) or _policy_figure_hit(t)


# Kata kunci komoditas/makro -> sektor terdampak (untuk berita global)
SECTOR_HINTS = {
    "oil": ["ADRO", "PTBA", "MEDC", "PGAS", "ITMG", "AKRA"],
    "minyak": ["MEDC", "PGAS", "AKRA"],
    "coal": ["ADRO", "PTBA", "ITMG", "INDY"],
    "batu bara": ["ADRO", "PTBA", "ITMG", "INDY"],
    "nickel": ["INCO", "ANTM", "NCKL", "MDKA"],
    "nikel": ["INCO", "ANTM", "NCKL", "MDKA"],
    "gold": ["MDKA", "ANTM"],
    "emas": ["MDKA", "ANTM"],
    # bare "rupiah" DIBUANG (audit 2026-07-13) — cocok di HAMPIR SEMUA berita berharga-Rupiah
    # ("200 ribu rupiah/bulan") krn dipakai sbg satuan mata uang, salah-tag BBCA/BBRI/BMRI
    # pada berita non-saham (mis. tarif TransJakarta). Ganti frasa pergerakan kurs spesifik.
    "rupiah menguat": ["BBCA", "BBRI", "BMRI"],
    "rupiah melemah": ["BBCA", "BBRI", "BMRI"],
    "nilai tukar rupiah": ["BBCA", "BBRI", "BMRI"],
    "kurs rupiah": ["BBCA", "BBRI", "BMRI"],
    "rate": ["BBCA", "BBRI", "BMRI", "BBNI"],
    "suku bunga": ["BBCA", "BBRI", "BMRI", "BBNI"],
}
# CATATAN: event geopolitik (Hormuz, perang, OPEC, tarif Trump) SENGAJA tidak di-tag
# langsung ke saham — efeknya LEWAT harga minyak/komoditas & sentimen risiko. Beritanya
# tetap scope=global → otomatis masuk konteks SEMUA analisis; agen yang menalar rantainya.


def _is_noise(title: str) -> bool:
    """True utk headline promosi/ramalan/clickbait yang bisa menyesatkan sentimen."""
    t = title.lower()
    return any(p in t for p in NOISE_PATTERNS)


def _wb(words, t: str) -> int:
    """Hitung kata/frasa yang muncul sbg KATA UTUH (word-boundary) — cegah substring nyasar
    (mis. 'war'↔'warisan/warga', 'demo'↔'demokrasi', 'rate'↔'corporate')."""
    return sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", t))


def _sentiment(text: str) -> tuple[str, int]:
    t = text.lower()
    # Frasa konteks (bobot 2) dulu — substring aman krn spesifik. Kata TUNGGAL pakai word-boundary.
    score = (2 * sum(p in t for p in POS_PHRASES)
             - 2 * sum(p in t for p in NEG_PHRASES)
             + _wb(POSITIVE_WORDS, t)
             - _wb(NEGATIVE_WORDS, t))
    # Magnitudo 3 hanya jika peristiwa krisis NYATA: ≥2 indikator besar ATAU 1 term sangat kuat
    # — dan TIDAK ada frasa pereda (krisis yang disebut aman/selesai bukan darurat).
    hi = ((_wb(HIGH_IMPACT, t) >= 2 or _wb(STRONG_CRISIS, t) >= 1)
          and not any(p in t for p in ALL_CLEAR))
    # TANPA kata krisis, cap magnitudo ±2 (audit 2026-07-11: "rupiah melemah" = frasa -2 +
    # kata "melemah" -1 = -3 → headline kurs RUTIN menyamar jadi krisis & bisa memicu siklus
    # DARURAT yang ambangnya ±3). Magnitudo 3 = hak eksklusif krisis nyata.
    if score > 0:
        return "positive", 3 if hi else min(2, score)
    if score < 0:
        return "negative", -3 if hi else max(-2, score)
    return ("negative", -3) if hi else ("neutral", 0)  # krisis nyata + netral → risk-off


def _tag_tickers(text: str) -> list[str]:
    """Tag emiten via KATA UTUH (word-boundary), bukan substring — cegah false-positive
    (mis. 'buka'→BUKA, 'corporate rate'→bank, 'warga'/'jago'). Kode 4-huruf hanya cocok bila
    ditulis HURUF BESAR sbg kata utuh (konvensi ticker: 'BUKA', '(BBCA)'); nama perusahaan
    cocok sbg kata utuh lowercase."""
    t = text.lower()
    found: set[str] = set()
    for code, names in COMPANY_NAMES.items():
        if (re.search(rf"\b{code}\b", text)
                or any(re.search(rf"\b{re.escape(n.strip())}\b", t) for n in names)):
            found.add(code)
    for kw, codes in SECTOR_HINTS.items():
        if re.search(rf"\b{re.escape(kw.strip())}\b", t):
            found.update(codes)
    return sorted(found)


def _fetch_feed(url: str, scope: str) -> list[dict]:
    """Ambil + parse satu feed (I/O jaringan). Hanya membaca, aman paralel."""
    items: list[dict] = []
    try:
        # Fetch via requests dgn TIMEOUT (feedparser.parse(url) sendiri TANPA timeout
        # bisa menggantung selamanya kalau server lambat → hindari hang).
        resp = requests.get(url, timeout=12, headers=_HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception:  # noqa: BLE001
        return items
    source = feed.feed.get("title", url) if hasattr(feed, "feed") else url
    for entry in feed.entries[:25]:
        title = entry.get("title", "")
        summary = re.sub("<[^<]+?>", "", entry.get("summary", ""))[:400]
        link = entry.get("link", "")
        if not title or not link or _is_noise(title):  # buang promo/clickbait
            continue
        blob = f"{title} {summary}"
        sentiment, impact = _sentiment(blob)
        tickers = _tag_tickers(blob)
        if not _is_relevant(blob, tickers, impact):  # buang off-topic (olahraga/hiburan/dll)
            continue
        # Berita makro pasar-luas dari feed lokal → naikkan ke global (lihat MARKET_WIDE).
        sc = "global" if scope == "local" and _is_market_wide(blob) else scope
        items.append({
            "ts": _entry_time(entry),  # waktu terbit ASLI → filter umur akurat
            "scope": sc, "source": source, "title": title,
            "url": link, "summary": summary, "tickers": tickers,
            "sentiment": sentiment, "impact": impact,
        })
    return items


# Panen kode emiten IPO dari berita: konvensi IDX = kode 4-huruf dalam kurung, mis "(WBSA)".
_IPO_HINTS = ("ipo", "melantai", "pencatatan perdana", "saham perdana",
              "penawaran umum perdana", "tercatat di bursa", "listing", "debut bursa",
              "calon emiten", "bookbuilding", "masa penawaran")
_CODE_RE = re.compile(r"\(([A-Z]{4})\)")
_CODE_STOP = {"KSEI", "RUPS", "BUMN", "APBN", "OJKK"}  # akronim, bukan ticker (sisanya dibuang daftar-mati)
# Sudah/segera diperdagangkan → kode boleh masuk universe (tradeable).
_LISTED_HINTS = ("resmi tercatat", "tercatat di bursa", "mulai diperdagangkan", "debut",
                 "listing perdana", "dapat dibeli", "sudah ipo", "ipo hari ini", "melantai di bursa")
# Masih AKAN datang → belum ada harga di yfinance → JANGAN masuk universe (nanti ditandai mati),
# cukup di-highlight.
_UPCOMING_HINTS = ("akan ipo", "bakal ipo", "berencana ipo", "siap ipo", "calon emiten",
                   "segera melantai", "bakal melantai", "akan melantai", "masa penawaran",
                   "bookbuilding", "book building", "menawarkan saham", "target dana ipo",
                   "pipeline ipo", "ipo mendatang", "rencana ipo", "incar ipo", "antrean ipo")

UPCOMING_FILE = config.DATA_DIR / "upcoming_ipos.json"
UPCOMING_KEEP = 25
UPCOMING_MAX_AGE_DAYS = 45


def _load_upcoming() -> list[dict]:
    try:
        return json.loads(UPCOMING_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def upcoming_ipos() -> list[dict]:
    """IPO yang AKAN datang (belum listing) untuk di-highlight. Terbaru dulu, segar & tak kembar."""
    cut = (datetime.now(timezone.utc) - timedelta(days=UPCOMING_MAX_AGE_DAYS)).isoformat()
    items = sorted((i for i in _load_upcoming() if i.get("ts", "") >= cut),
                   key=lambda x: x.get("ts", ""), reverse=True)
    seen: set[str] = set()
    out: list[dict] = []
    for i in items:
        codes = i.get("codes") or []
        k = codes[0] if codes else _norm_title(i.get("title", ""))  # dedup utama by kode emiten
        if k in seen:
            continue
        seen.add(k)
        out.append(i)
    return out


def _norm_title(t: str) -> str:
    return re.sub(r"\W+", "", (t or "").lower())[:55]


def _add_upcoming(news_items: list[dict]) -> int:
    store = _load_upcoming()
    seen = {_norm_title(i.get("title", "")) for i in store}
    added = 0
    for it in news_items:
        key = _norm_title(it["title"])
        if key in seen:
            continue
        codes = sorted({c for c in _CODE_RE.findall(f"{it['title']} {it.get('summary','')}")
                        if c not in _CODE_STOP})
        store.append({"title": it["title"], "url": it["url"], "source": it["source"],
                      "ts": it["ts"], "codes": codes})
        seen.add(key)
        added += 1
    store.sort(key=lambda x: x.get("ts", ""), reverse=True)
    UPCOMING_FILE.write_text(json.dumps(store[:UPCOMING_KEEP], ensure_ascii=False), encoding="utf-8")
    return added


def add_official_ipos(records: list[dict]) -> int:
    """Masukkan IPO RESMI e-IPO ke store highlight. Otoritatif: gantikan entri berita
    untuk kode yang sama (records = [{code, company, status}, ...])."""
    now = datetime.now(timezone.utc).isoformat()
    officials = [{
        "title": f"{r['company']} ({r['code']}) — {r['status']}",
        "url": "https://e-ipo.co.id/id/ipo/index",
        "source": "e-IPO (resmi BEI)", "ts": now,
        "codes": [r["code"]], "status": r["status"], "official": True,
    } for r in records]
    off_codes = {r["code"] for r in records}
    # simpan entri berita lama yang kodenya TIDAK ada di daftar resmi
    kept = [i for i in _load_upcoming()
            if not (i.get("codes") and i["codes"][0] in off_codes)]
    merged = sorted(officials + kept, key=lambda x: x.get("ts", ""), reverse=True)
    UPCOMING_FILE.write_text(json.dumps(merged[:UPCOMING_KEEP], ensure_ascii=False), encoding="utf-8")
    return len(officials)


def _harvest_ipo_codes(items: list[dict]) -> None:
    """Pisahkan IPO LISTED (→ universe, tradeable) vs UPCOMING (→ highlight, belum diperdagangkan)."""
    listed: set[str] = set()
    upcoming: list[dict] = []
    for it in items:
        blob = f"{it.get('title', '')} {it.get('summary', '')}".lower()
        if not any(h in blob for h in _IPO_HINTS):
            continue
        raw = f"{it.get('title', '')} {it.get('summary', '')}"
        codes = [c for c in _CODE_RE.findall(raw) if c not in _CODE_STOP]
        if any(h in blob for h in _UPCOMING_HINTS) and not any(h in blob for h in _LISTED_HINTS):
            upcoming.append(it)            # AKAN datang → highlight saja
        else:
            listed.update(codes)           # sudah/segera listing → boleh ditradingkan
    if listed and universe.add_discovered(listed):
        repo.log("engine", "fetch", f"IPO listing terdeteksi: {', '.join(sorted(listed))}")
    if upcoming and _add_upcoming(upcoming):
        repo.log("engine", "fetch", f"IPO mendatang ter-highlight: {len(upcoming)} berita")


def refresh_news() -> int:
    jobs = ([(u, "local") for u in config.NEWS_FEEDS_LOCAL] +
            [(u, "global") for u in config.NEWS_FEEDS_GLOBAL] +
            # YouTube (pidato/kebijakan) → scope local, auto-naik global kalau market-wide
            [(u, "local") for u in getattr(config, "NEWS_YOUTUBE", [])] +
            # OSINT: Google News RSS per-topik ekonomi/makro
            [(gnews_url(q), "global") for q in config.NEWS_GOOGLE_TOPICS])
    # Fetch semua feed PARALEL (banyak berita sekaligus)...
    all_items: list[dict] = []
    with ThreadPoolExecutor(max_workers=NEWS_WORKERS) as ex:
        for items in ex.map(lambda j: _fetch_feed(*j), jobs):
            all_items.extend(items)
    # ...lalu simpan SEKUENSIAL (hindari rebutan tulis SQLite).
    total = sum(1 for it in all_items if repo.save_news(it))
    _harvest_ipo_codes(all_items)  # IPO baru → masuk universe otomatis
    repo.log("engine", "fetch", f"berita baru tersimpan: {total} (dari {len(jobs)} sumber)")
    return total


def fetch_ticker_news(ticker: str) -> int:
    """OSINT: tarik berita Google News SPESIFIK untuk satu saham (saat dianalisis agen).
    Di-throttle agar tidak berulang untuk ticker yang sama."""
    now = time.time()
    if now - _ticker_news_at.get(ticker, 0) < TICKER_NEWS_TTL:
        return 0
    _ticker_news_at[ticker] = now
    names = COMPANY_NAMES.get(ticker, [])
    query = f"{ticker} saham" + (f" {names[0]}" if names else "")
    # 2 sumber CEPAT per-saham: Google News (segar, when:48h) + Yahoo Finance RSS (spesifik emiten).
    items = _fetch_feed(gnews_url(query, fresh_hours=48), "ticker")
    items += _fetch_feed(yahoo_ticker_url(ticker), "ticker")
    # paksa tag ticker ini (kadang nama tak terdeteksi otomatis)
    saved = 0
    for it in items:
        if ticker not in it["tickers"]:
            it["tickers"] = sorted(set(it["tickers"] + [ticker]))
        if repo.save_news(it):
            saved += 1
    if saved:
        repo.log("engine", "fetch", f"OSINT berita {ticker}: +{saved} dari Google News",
                 ticker=ticker)
    return saved
