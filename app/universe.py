"""Daftar universe saham IDX — DIKELOLA OLEH SCRIPT, bukan oleh AI agent.

Prioritas sumber:
  1. file `data/idx_universe.txt` (1 ticker per baris) — drop daftar 900 penuh di sini.
  2. env UNIVERSE_URL (CSV/teks berisi kode) — di-fetch lalu di-cache ke file.
  3. SEED bawaan (di bawah) — daftar luas saham IDX likuid lintas sektor.

Saham dengan harga < MIN_PRICE (default Rp50) otomatis diabaikan saat fetch harga,
jadi gocap/junk dari ekor ~900 saham tidak ikut dianalisis.

CLI:
  python -m app.universe              # tampilkan & tulis SEED ke file cache
  python -m app.universe --fetch      # coba ambil dari UNIVERSE_URL lalu cache
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date

import config

# ------------------------------------------------------------------ SEED
# Daftar kode 4-huruf IDX yang umum & likuid (superset dari WATCHLIST).
# Tidak perlu lengkap 900 — kode tak valid otomatis dilewati saat fetch.
SEED: list[str] = [
    # Perbankan & finansial
    "BBCA","BBRI","BMRI","BBNI","BRIS","ARTO","BBTN","BTPS","BNGA","BBYB","MEGA",
    "PNBN","BJBR","BJTM","BANK","AGRO","BBKP","NISP","BNLI","BDMN","BTPN","BBMD",
    "BBSI","BBHI","BGTG","BVIC","SDRA","BNII","MCOR","AMAR","BACA","MASB","BSIM",
    "ADMF","BFIN","CFIN","MFIN","WOMF","PNLF","PNIN","ABDA","TUGU","ASRM","LIFE",
    "AMAG","JMAS","BPII",
    # Telko, menara & teknologi
    "TLKM","EXCL","ISAT","FREN","GOTO","EMTK","MTEL","TOWR","TBIG","DCII","WIFI",
    "BELI","MLPT","MTDL","DMMX","EDGE","KREN","DIVA","NFCX","HDIT","ENVY","GLVA",
    # Konsumer
    "UNVR","ICBP","INDF","MYOR","GGRM","HMSP","SIDO","ULTJ","KEJU","CMRY","ROTI",
    "STTP","CLEO","GOOD","KINO","TCID","DLTA","MLBI","CAMP","AISA","FOOD","WIIM",
    "AMRT","MAPI","MAPA","ACES","ERAA","RALS","LPPF","MPPA","RANC","CSAP","HERO",
    "MIDI","HRTA","ITIC",
    # Energi, batu bara & migas
    "ADRO","PTBA","ITMG","INDY","MEDC","PGAS","AKRA","HRUM","BUMI","BYAN","DSSA",
    "DOID","PTRO","RAJA","ELSA","ENRG","ADMR","GEMS","BSSR","MBAP","TOBA","BIPI",
    "ARII","PKPK","SMMT","FIRE","RMKE","CUAN","PGEO","BREN","KKGI","MYOH","ITMA",
    "GTBO","SGER","APEX",
    # Tambang logam & mineral
    "ANTM","INCO","TINS","MDKA","NCKL","AMMN","MBMA","BRMS","PSAB","DKFT","ZINC",
    "CITA","SMRU","IFSH","NICE","HRUM",
    # Otomotif & industri
    "ASII","UNTR","AUTO","GJTL","IMAS","DRMA","SMSM","BOLT","GDYR","INDS","MASA",
    "BRAM","HEXA",
    # Semen & material bangunan
    "SMGR","INTP","SMBR","SMCB","WTON","WSBP","TOTO","ARNA","AMFG","MARK","KIAS",
    # Konstruksi & infrastruktur
    "WIKA","PTPP","ADHI","WSKT","JSMR","PPRE","NRCA","TOTL","ACST","SSIA","IDPR",
    # Properti
    "BSDE","CTRA","PWON","SMRA","PANI","APLN","DMAS","KIJA","ASRI","LPKR","DILD",
    "MKPI","BEST","GWSA","RDTX","MTLA","BKSL","DUTI","JRPT","LPCK","PPRO","MDLN",
    "CBDK",
    # Kimia & industri dasar
    "BRPT","TPIA","ESSA","AVIA","INCI","EKAD","SRSN","UNIC","DPNS","AGII","BMSR",
    "INKP","TKIM","FASW","SPMA","KDSI","ALDO",
    # Unggas & agribisnis
    "CPIN","JPFA","MAIN","AALI","LSIP","SSMS","DSNG","TAPG","SGRO","BWPT","ANJT",
    "TBLA","SIMP","SMAR","CSRA","FAPA",
    # Kesehatan & farmasi
    "KLBF","KAEF","INAF","SILO","HEAL","MIKA","PRDA","SAME","SOHO","PEHA","IRRA",
    "DGNS","SRAJ","BMHS","CARE","RSGK","HALO",
    # Media & hiburan
    "MNCN","SCMA","FILM","PZZA","BMTR","MSIN","VIVA","TMPO","FORU","IPTV",
    # Transportasi & logistik
    "SMDR","TMAS","ASSA","BIRD","GIAA","WEHA","NELY","SOCI","IPCC","CMPP","PSSI",
    # Holding & investasi
    "SRTG","BHIT","BNBR","POOL","APIC","TRIM",
    # Pariwisata/hotel & lain-lain
    "PJAA","PNSE","INPP","BAYU","JIHD","HOME","MINA",
    # IPO/baru & beragam
    "RATU","DAAZ","HOPE","MUTU","GRPM","MANG","GTRA","OBAT","VKTR","HILL","CYBR",
    "WINE","BAUT","SMLE","KETR","HALO",
]


# IPO baru / saham yang sering belum ada di dataset lama (termasuk WBSA, AADI).
RECENT_IPOS: list[str] = [
    "WBSA", "AADI", "RATU", "DAAZ", "CBDK", "BREN", "CUAN", "AMMN", "PANI", "MBMA",
    "NCKL", "PGEO", "KETR", "VKTR", "HILL", "CYBR", "MANG", "GTRA", "OBAT", "NICE",
    "NSSS", "MUTU", "HOPE", "GRPM", "FUTR", "AYAM", "COCO", "SMLE", "WINE", "BAUT",
    "RMKO", "MSIE", "GRIA", "BABY", "ERAL", "OMED", "PTMP", "MENN", "ASLI", "ISEA",
    "GUNA", "MEJA", "LOPI", "MKAP", "AEGS", "CHEK", "CRSN", "LMAX", "SOLA", "TYRE",
    "DOSS", "HBAT", "GLOW", "STRK", "RSCH", "MHKI", "KOCI", "SMGA", "VISI", "PART",
    # IPO terbaru 2025-2026 (hasil riset; sisanya dipanen otomatis dari berita)
    "WBSA", "SUPA", "CDIA", "COIN", "MERI", "ASHA", "LFLO", "BDKR", "PJHB", "TRUE",
]


# ------------------------------------------------------------------ daftar-mati
# Ticker delisted/suspend yang yfinance tolak ("no price data / delisted") dicatat
# di sini agar TIDAK diminta ulang tiap siklus (hemat request + buang spam log).
DEAD_FILE = config.DATA_DIR / "dead_tickers.json"
DEAD_THRESHOLD = 3      # gagal X siklus berturut → dianggap delisted, berhenti diminta
DEAD_RECHECK_DAYS = 14  # ponytail: setelah ini, coba lagi sekali (kalau cuma suspend sementara)

# IPO yang dipanen otomatis dari berita (kode dalam tanda kurung, mis. "(WBSA)").
DISCOVERED_FILE = config.DATA_DIR / "discovered_ipos.txt"
# Kapan tiap kode discovered PERTAMA KALI terlihat — dipakai IPO_GRACE_DAYS di bawah.
DISCOVERED_AT_FILE = config.DATA_DIR / "discovered_at.json"
# IPO baru: status berita "book-building closed" bisa MENDAHULUI listing sungguhan
# beberapa minggu → yfinance gagal (belum trading) BUKAN berarti delisted. Beri masa
# tenggang sebelum ikut kena daftar-mati, supaya begitu benar-benar listing (kapan pun
# dalam masa ini) langsung otomatis kepakai siklus berikutnya, tanpa nunggu 14 hari.
IPO_GRACE_DAYS = 30


def _load_dead() -> dict:
    try:
        return json.loads(DEAD_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_dead(d: dict) -> None:
    try:
        DEAD_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _load_discovered_at() -> dict:
    try:
        return json.loads(DISCOVERED_AT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_discovered_at(d: dict) -> None:
    try:
        DISCOVERED_AT_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def dead_set() -> set[str]:
    """Ticker yang sudah dianggap mati (lewati saat fetch). Auto re-test tiap DEAD_RECHECK_DAYS.
    IPO yang baru DITEMUKAN (discovered_at) dalam IPO_GRACE_DAYS terakhir DIKECUALIKAN —
    gagal fetch di masa itu wajar (belum listing), bukan sinyal delisted."""
    today = date.today().toordinal()
    at = _load_discovered_at()
    return {t for t, rec in _load_dead().items()
            if rec.get("misses", 0) >= DEAD_THRESHOLD
            and today - rec.get("day", today) <= DEAD_RECHECK_DAYS
            and today - at.get(t, 0) > IPO_GRACE_DAYS}


def update_dead(missed, alive) -> None:
    """Catat hasil 1 siklus fetch: yang hidup dihapus dari daftar, yang gagal ditambah miss."""
    d = _load_dead()
    today = date.today().toordinal()
    for t in alive:
        d.pop(t, None)
    for t in missed:
        rec = d.get(t) or {"misses": 0}
        rec["misses"] = rec.get("misses", 0) + 1
        rec["day"] = today
        d[t] = rec
    _save_dead(d)


def _discovered() -> list[str]:
    if DISCOVERED_FILE.exists():
        try:
            return _clean(DISCOVERED_FILE.read_text(encoding="utf-8").splitlines())
        except Exception:  # noqa: BLE001
            return []
    return []


def add_discovered(codes) -> int:
    """Tambah kode IPO baru (hasil panen berita) ke universe. Yang salah/mati nanti dibuang daftar-mati
    (kecuali masih dalam IPO_GRACE_DAYS sejak pertama ditemukan)."""
    new = _clean(codes)
    if not new:
        return 0
    have = set(_discovered()) | set(SEED) | set(RECENT_IPOS) | {c.upper() for c in config.WATCHLIST}
    add = [c for c in new if c not in have]
    if add:
        with DISCOVERED_FILE.open("a", encoding="utf-8") as f:
            f.write("\n".join(add) + "\n")
        at = _load_discovered_at()
        today = date.today().toordinal()
        for c in add:
            at.setdefault(c, today)
        _save_discovered_at(at)
    return len(add)

# Sumber daftar ticker IDX (folder per-ticker) via GitHub API.
GITHUB_SOURCE = "https://api.github.com/repos/faisalburhanudin/idx/contents/stocks"


def fetch_from_github() -> list[str]:
    """Ambil daftar ticker dari dataset IDX di GitHub (script, bukan agen). Aman gagal."""
    try:
        import requests
        r = requests.get(GITHUB_SOURCE, timeout=25,
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        names = [it.get("name", "") for it in r.json() if isinstance(it, dict)]
        codes = [re.sub(r"\.(csv|json|txt)$", "", n) for n in names]
        return _clean(codes)
    except Exception:  # noqa: BLE001
        return []


def _clean(codes) -> list[str]:
    seen: dict[str, None] = {}
    for c in codes:
        c = (c or "").strip().upper().replace(".JK", "")
        if re.fullmatch(r"[A-Z]{3,5}", c):
            seen.setdefault(c, None)
    return list(seen.keys())


def _from_file() -> list[str]:
    f = config.UNIVERSE_FILE
    if f.exists():
        try:
            return _clean(f.read_text(encoding="utf-8").splitlines())
        except Exception:  # noqa: BLE001
            return []
    return []


def fetch_from_web() -> list[str]:
    """Best-effort: ambil daftar dari env UNIVERSE_URL (CSV/teks). Aman gagal."""
    url = os.getenv("UNIVERSE_URL", "").strip()
    if not url:
        return []
    try:
        import requests
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        codes = re.findall(r"\b([A-Z]{3,5})\b", r.text)
        return _clean(codes)
    except Exception:  # noqa: BLE001
        return []


def save_to_file(codes: list[str]) -> None:
    config.UNIVERSE_FILE.write_text("\n".join(codes) + "\n", encoding="utf-8")


def get_universe() -> list[str]:
    """Universe efektif yang dipakai engine. Gabung file+seed+IPO+discovered, buang yang mati."""
    if not config.USE_FULL_UNIVERSE:
        uni = _clean(config.WATCHLIST)
    else:
        # Gabung SEMUA sumber (bukan hanya file) supaya IPO baru & hasil panen selalu ikut.
        uni = _clean(_from_file() + SEED + RECENT_IPOS + _discovered() + list(config.WATCHLIST))
    dead = dead_set()
    return [t for t in uni if t not in dead]


def build_full(verbose: bool = True) -> list[str]:
    """Gabung GitHub + UNIVERSE_URL + SEED + IPO baru + WATCHLIST → tulis ke file."""
    gh = fetch_from_github()
    web = fetch_from_web()
    merged = _clean(gh + web + SEED + RECENT_IPOS + list(config.WATCHLIST))
    save_to_file(merged)
    if verbose:
        print(f"GitHub {len(gh)} + web {len(web)} + SEED/IPO digabung → "
              f"{len(merged)} ticker ke {config.UNIVERSE_FILE}")
    return merged


def main() -> None:
    if "--fetch" in sys.argv:
        build_full()
        return
    uni = get_universe()
    save_to_file(uni)
    print(f"Universe: {len(uni)} ticker -> {config.UNIVERSE_FILE}")
    print(", ".join(uni[:30]), "...")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
