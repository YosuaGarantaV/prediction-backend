"""Proxy aliran dana asing + breadth pasar — dihitung oleh SCRIPT dari data harga.

CATATAN JUJUR: ini PROXY, bukan data asing resmi (KSEI/broker). Tidak ada API gratis
untuk net buy/sell asing real-time. Proxy ini memperkirakan tekanan beli/jual dengan
melihat turnover (volume×harga) × arah pada big-cap yang biasa ditransaksikan asing,
plus market breadth (advancers vs decliners). Cukup untuk konteks risk-on/risk-off.
Untuk data asing presisi, butuh sumber berbayar (Stockbit/RTI/KSEI).
"""
from __future__ import annotations

from app import repo
from app.data import msci

# Proksi arus asing berbasis KONSTITUEN MSCI Indonesia (tempat dana asing terkonsentrasi),
# ditimbang bobot indeksnya — flow ke BBCA/BMRI (bobot ~22%) lebih berarti dari small-cap.
FOREIGN_FAVORITES = set(msci.MSCI_INDONESIA.keys())


def compute_flow() -> dict | None:
    """Hitung proxy arus asing + breadth dari quote tersimpan, simpan snapshot."""
    quotes = repo.all_quotes()
    if not quotes:
        return None

    advancers = sum(1 for q in quotes if (q.get("change_pct") or 0) > 0)
    decliners = sum(1 for q in quotes if (q.get("change_pct") or 0) < 0)
    breadth_up = round(advancers / (advancers + decliners) * 100, 1) if (advancers + decliners) else 50.0

    net_value = 0.0       # miliar Rp (estimasi tekanan beli-jual, ditimbang bobot MSCI)
    contribs: list[tuple[str, float]] = []
    for q in quotes:
        if q["ticker"] not in FOREIGN_FAVORITES:
            continue
        chg = (q.get("change_pct") or 0.0) / 100.0
        turnover = (q.get("price") or 0.0) * (q.get("volume") or 0.0)  # nilai diperdagangkan
        w = 1.0 + msci.weight(q["ticker"]) / 10.0   # bobot MSCI memperbesar pengaruh
        contrib = chg * turnover / 1e9 * w   # miliar Rp (tertimbang)
        net_value += contrib
        contribs.append((q["ticker"], contrib))

    # skor sentimen -1..+1 dari arah + breadth
    fav_up = sum(1 for t, c in contribs if c > 0)
    fav_tot = len(contribs) or 1
    flow_score = round(((fav_up / fav_tot) - 0.5) * 2 * 0.6 + ((breadth_up / 100) - 0.5) * 2 * 0.4, 2)

    contribs.sort(key=lambda x: x[1], reverse=True)
    top_inflow = ", ".join(f"{t}(+{c:.1f}M)" for t, c in contribs[:3] if c > 0) or "-"
    top_outflow = ", ".join(f"{t}({c:.1f}M)" for t, c in contribs[-3:] if c < 0) or "-"

    if flow_score > 0.2 and breadth_up > 55:
        label = "NET BUY (risk-on) — big-cap & pasar menguat"
    elif flow_score < -0.2 and breadth_up < 45:
        label = "NET SELL (risk-off) — tekanan jual asing/pasar"
    else:
        label = "Netral / campuran"

    f = {
        "foreign_proxy": round(net_value, 1), "flow_score": flow_score,
        "breadth_up": breadth_up, "advancers": advancers, "decliners": decliners,
        "label": label, "top_inflow": top_inflow, "top_outflow": top_outflow,
    }
    repo.save_flow(f)
    repo.log("engine", "fetch", f"proxy arus asing: {label} (score {flow_score}, "
             f"breadth {breadth_up}% naik, net ~Rp{net_value:.0f}M)")
    return f


def flow_brief() -> str:
    """Ringkasan untuk prompt agen."""
    f = repo.latest_flow()
    if not f:
        return "(proxy arus asing belum tersedia)"
    return (f"Proxy arus asing/big-cap: {f['label']} (skor {f['flow_score']}). "
            f"Breadth: {f['breadth_up']}% saham naik ({f['advancers']} naik / {f['decliners']} turun). "
            f"Inflow proxy: {f['top_inflow']}. Outflow: {f['top_outflow']}.")


def ihsg_green_streak() -> int:
    """Berapa hari beruntun IHSG naik (dari close harian tersimpan). 0 bila data kurang."""
    try:
        rows = repo.get_conn().execute(
            "SELECT close FROM prices WHERE ticker='IHSG' ORDER BY ts DESC LIMIT 6"
        ).fetchall()
        closes = [r["close"] for r in rows]  # terbaru dulu
        n = 0
        for newer, older in zip(closes, closes[1:]):
            if newer > older:
                n += 1
            else:
                break
        return n
    except Exception:  # noqa: BLE001
        return 0


def ihsg_uptrend() -> tuple[bool, float, float]:
    """Tren IHSG UTUH? = close terakhir > SMA5-nya. Lebih TAHAN NOISE dari green-streak yang
    patah oleh SATU hari merah. Return (uptrend, last, sma5). ROOT CAUSE Lv.5→Lv.2 (audit
    2026-07-13): 8 Jul IHSG dip 5986→5873 mematahkan streak → local_reversal OFF → 28 DOWN:1 UP
    dibuat saat tren NAIK masih utuh (5873 > SMA5 5844) → semua rugi saat pasar mantul balik.
    SMA5 menangkap 'dip dalam uptrend' yang green-streak lewatkan."""
    try:
        rows = repo.get_conn().execute(
            "SELECT close FROM prices WHERE ticker='IHSG' ORDER BY ts DESC LIMIT 6"
        ).fetchall()
        closes = [r["close"] for r in rows]  # terbaru dulu
        if len(closes) < 5:
            return False, 0.0, 0.0
        last = closes[0]
        sma5 = sum(closes[1:6]) / len(closes[1:6])   # 5 hari SEBELUM hari ini
        return last > sma5, last, round(sma5, 1)
    except Exception:  # noqa: BLE001
        return False, 0.0, 0.0


def local_reversal() -> tuple[bool, str]:
    """Bukti PEMBALIKAN/TREN REZIM LOKAL (Aturan 79, deterministik): pasar lokal sedang naik
    → indikator bearish per-saham LAG → taruhan DOWN lemah rawan salah. lean GLOBAL bisa tetap
    risk-off saat IHSG decoupling rally (kasus nyata 8-10 Jul 2026) → rezim lokal menang.

    Trigger (salah satu tren + breadth+flow tak-negatif): (a) IHSG >=2 hari hijau (rally jelas),
    ATAU (b) IHSG di ATAS SMA5-nya (tren naik utuh walau ada 1 hari dip — menutup celah 8 Jul
    saat streak patah tapi tren masih naik, biang 28 DOWN rugi)."""
    f = repo.latest_flow()
    if not f:
        return False, ""
    streak = ihsg_green_streak()
    uptrend, last, sma5 = ihsg_uptrend()
    breadth_ok = (f.get("breadth_up") or 0) >= 58     # sedikit longgar dari 60 (dip-day breadth turun)
    flow_ok = (f.get("flow_score") or 0) >= -0.1      # toleransi noise kecil di hari dip
    trend_ok = streak >= 2 or uptrend
    on = trend_ok and breadth_ok and flow_ok
    trg = "streak" if streak >= 2 else ("SMA5" if uptrend else "-")
    note = (f"IHSG {streak}hr hijau / {last:.0f} vs SMA5 {sma5:.0f} (tren:{trg}), "
            f"breadth {f.get('breadth_up')}%, flow {f.get('flow_score')}")
    return on, note
