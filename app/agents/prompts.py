"""Susun konteks (fitur teknikal + berita + makro) menjadi prompt untuk agen."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app import events, model, repo
from app.data import flow, fundamentals, idxflow, premarket
from app.agents.knowledge import build_knowledge_prompt


def _model_signal(feats: dict) -> str:
    p = model.predict_proba(feats)
    if p is None:
        return "(model belum dilatih)"
    return f"P(naik {model.HORIZON} hari) = {p * 100:.0f}%  (>50% condong naik, <50% turun)"


def _premarket_block() -> str:
    try:
        return premarket.brief_line()
    except Exception:  # noqa: BLE001
        return "(briefing global belum tersedia)"


def _macro_block() -> str:
    rows = repo.all_macro()
    if not rows:
        return "(belum ada data makro)"
    return "\n".join(
        f"- {r['name']} ({r['symbol']}): {r['price']} ({(r['change_pct'] or 0):+.2f}%)"
        for r in rows
    )


def _track_record_block(ticker: str) -> str:
    """Rekam jejak agen di saham INI — memori per-saham (jangan ulangi kesalahan yang sama)."""
    tr = repo.ticker_track_record(ticker)
    if not tr:
        return "(belum ada riwayat prediksi di saham ini)"
    parts = []
    for d, v in tr["by_direction"].items():
        parts.append(f"{d} {v['win']}/{v['n']} benar")
    lines = ["Ringkas: " + ", ".join(parts)]
    for r in tr["recent"][:4]:
        ok = "BENAR" if r["outcome"] == "win" else "SALAH"
        lines.append(f"- {r['direction']} {r['probability']:.0f}% → {ok} "
                     f"({(r['actual_pct'] or 0):+.1f}%)")
    return "\n".join(lines)


def _news_block(ticker: str) -> str:
    items = repo.news_for_ticker(ticker, limit=8)
    if not items:
        return "(belum ada berita relevan)"
    out = []
    for n in items:
        out.append(f"- [{n['scope']}|{n['sentiment']}|impact {(n['impact'] or 0):+d}] "
                   f"{n['title']}")
    return "\n".join(out)


def system_prompt() -> str:
    # Basis pengetahuan + pelajaran + KALIBRASI historis (anti-overfit).
    # AGENT_MODE: + digest performa 14 hari (belajar dari minggu pipeline); lessons mentah
    # diciutkan 10→5 sebagai kompensasi token (digest = sinyal agregat, lebih padat).
    import config as _cfg
    from app.agents.skill import calibration_guidance, performance_digest
    # agent mode 3 lesson global (offset token: agen kini punya catatan per-saham sendiri
    # via remember yang lebih relevan drpd lesson global)
    lessons = repo.recent_lessons(3 if _cfg.AGENT_MODE else 10)
    base = build_knowledge_prompt(lessons)
    parts = [base]
    if _cfg.AGENT_MODE:
        try:
            dig = performance_digest(14)
            if dig:
                parts.append(dig)
        except Exception:  # noqa: BLE001
            pass
    try:
        cal = calibration_guidance()
        if cal:
            parts.append(cal)
    except Exception:  # noqa: BLE001
        pass
    return "\n\n".join(parts)


def trader_system_prompt() -> str:
    """System prompt RINGKAS untuk TRADER/CTO (~460 token vs ~4700 token knowledge penuh).

    Analis SUDAH memakai knowledge base penuh & menyusun tesis → CTO cukup aturan keputusan
    inti + disiplin risiko. Ini memangkas ~10× token tiap percobaan CTO; krusial karena saat
    gagal, rantai CTO mencoba SEMUA provider (7×) — prompt 6rb-token ×7 = kuota harian jebol.
    Kalibrasi (kecil tapi penting) tetap diikutkan agar CTO tak overconfident.
    """
    from app.agents.skill import calibration_guidance
    base = (
        "Kamu TRADER/CTO saham IDX: disiplin, skeptis, penentu keputusan akhir. Tinjau tesis "
        "ANALIS, kritik bagian lemah, lalu putuskan arah + aksi. Prinsip inti:\n"
        "1. JANGAN beli falling knife (turun tajam + momentum/volume belum konfirmasi rebound).\n"
        "2. Hormati rezim makro: risk-off / asing net-jual berat → kurangi BUY, perkecil size.\n"
        "3. Valuasi ekstrem (PER>100 / PBV>12) → hindari BUY.\n"
        "4. Saham illikuid (turnover tipis) → jangan overconfident; keyakinan tinggi butuh bukti kuat.\n"
        "5. Keyakinan TERKALIBRASI, jangan overconfident; utamakan selaras sinyal model statistik.\n"
        "6. BUY hanya jika UP & probability>=60 & expected_pct>~0.6% (tutup fee). SELL bila punya "
        "posisi & tesis rusak / target / stop-loss / kelamaan. Selain itu HOLD.\n"
        "Berbasis bukti (teknikal/fundamental/berita/arus asing/MSCI), jangan mengarang. "
        "Balas HANYA JSON valid sesuai format yang diminta."
    )
    import config as _cfg
    if _cfg.TRADER_TOOLS:
        # Transfer pengetahuan trigger skrip lama ke NORMA agen (agen tetap pemegang keputusan):
        # tanpa ini CTO terbukti melewatkan dewan di zona abu-abu (KETR 62.3% agree=false, 2026-07-04).
        base += ("\nTOOL: WAJIB panggil consult_council SEBELUM memutuskan bila keyakinanmu "
                 "55-68%, ATAU kamu tak sepakat dengan analis, ATAU hendak BUY. Panggil "
                 "get_fundamentals bila klaim valuasi analis perlu dicek. Di luar kondisi itu "
                 "JANGAN panggil tool — putuskan langsung (hemat).")
    try:
        cal = calibration_guidance()
    except Exception:  # noqa: BLE001
        cal = ""
    return f"{base}\n\n{cal}" if cal else base


def analyst_user_prompt(ticker: str, quote: dict, note: str | None = None,
                        horizon: int | None = None, lean: bool = False) -> str:
    """`lean`=True (jalur tool): blok BESAR & sering-tak-menentukan (fundamental, kalender) TIDAK
    di-inject — analis memanggil tool jika perlu. Menghemat prompt dasar; sisanya (teknikal/berita/
    arus asing/makro = rezim) tetap di-inject karena hampir selalu relevan."""
    feats = json.loads(quote.get("features_json") or "{}") if quote else {}
    extra = ""
    if note:
        extra += f"\n\nKONTEKS PENTING: {note}"
    if horizon:
        extra += (f"\n\nHORIZON DIMINTA: persempit analisis & skenario ke {horizon} hari bursa "
                  f"ke depan (bukan default) — target & keyakinan harus realistis utk jendela itu.")
    if lean:
        fund_block = ("(tersedia via tool get_fundamentals — panggil HANYA jika menilai "
                      "valuasi / kelayakan jangka panjang)")
        catalyst_block = ("(tersedia via tool get_forward_catalysts — panggil HANYA jika arah "
                          "bergantung pada agenda ke depan)")
        # Versi lama kalimat ini berbunyi "kalau teknikal + berita + arus asing cukup, JANGAN
        # panggil tool" — hemat token, tapi akibatnya terukur: 2026-09-02 mayoritas tesis live
        # tercatat "0 tool-call", jadi analis cuma membaca blok yang disodorkan skrip. Itu
        # pipeline inject-all memakai nama agen. Arahannya dibalik: satu langkah pembuktian
        # WAJIB, sisanya bebas — pagarnya tetap TOOL_MAX_ROUNDS, bukan larangan.
        tool_hint = (
            "\n\nCARA KERJAMU (mode agen): data di atas adalah TITIK AWAL, bukan seluruh bukti. "
            "Kamu punya tool on-demand:\n"
            "- screen_market — sapu SELURUH saham likuid dengan syarat teknikal RUMUSANMU "
            "SENDIRI. Ini alat menemukan pola: uji dugaanmu ('apakah oversold + volume ramai "
            "sedang menyebar?'), lihat apakah gerakan saham ini bagian dari tema yang lebih luas, "
            "atau temukan setup yang belum disodorkan siapa pun.\n"
            "- track_record — win-rate NYATA tiap faktor di prediksi yang sudah selesai. Pakai "
            "untuk menimbang bobot faktor dengan catatan, bukan dengan perasaan.\n"
            "- search_news — cari katalis/tema di arsip berita (kata kunci bebas).\n"
            "- get_sector_peers / check_ticker — buktikan gerakan ini sektoral atau spesifik.\n"
            "- get_fundamentals / get_forward_catalysts — valuasi & agenda ke depan.\n"
            "- remember — simpan insight tahan-lama; suggest_ticker — usulkan saham lain yang "
            "temuanmu tunjukkan lebih layak.\n"
            "MINIMAL SATU tool harus kamu panggil sebelum menulis tesis: tesis yang hanya "
            "mendaur ulang blok di atas tidak menambah apa pun. Boleh beberapa tool sekaligus "
            "dalam satu putaran. Setelah bukti cukup, LANGSUNG tulis tesisnya — jangan mengulur "
            "putaran dan jangan memanggil tool yang sama dua kali.")
        own = repo.agent_notes(ticker)
        if own:
            tool_hint += ("\n\nCATATANMU SENDIRI ttg saham ini (kamu tulis via remember):\n- "
                          + "\n- ".join(own))
    else:
        fund_block = fundamentals.fundamentals_brief(ticker)
        catalyst_block = events.events_brief(21)
        tool_hint = ""
    return f"""Analisis saham {ticker} (IDX) untuk prediksi arah harga jangka pendek.{extra}

FUNDAMENTAL & STATUS MSCI:
{fund_block}

SINYAL MODEL STATISTIK (logreg terlatih walk-forward, AUC ~0.63 — pakai sebagai prior kuat):
{_model_signal(feats)}

DATA TEKNIKAL HARI INI:
{json.dumps(feats, ensure_ascii=False, separators=(',', ':'))}

BRIEFING GLOBAL SEMALAM (penggerak arah PEMBUKAAN IHSG — Wall St + Asia + rupiah + VIX):
{_premarket_block()}

SINYAL MAKRO GLOBAL TERKINI (minyak, emas, USD/IDR, US10Y, VIX, indeks dunia):
{_macro_block()}

ARUS ASING RESMI IDX — SAHAM INI (otoritatif, hari bursa terakhir; net beli asing = bullish):
{idxflow.foreign_brief(ticker)}

ARUS DANA ASING & BREADTH PASAR (proxy market-wide):
{flow.flow_brief()}

KALENDER KATALIS KE DEPAN (forward-looking — apa yang MUNGKIN terjadi):
{catalyst_block}

BERITA RELEVAN TERBARU (lokal + ekonomi global):
{_news_block(ticker)}

REKAM JEJAKMU DI SAHAM INI (hasil nyata prediksimu sebelumnya — belajar dari kesalahanmu sendiri; arah yang berulang kali salah butuh bukti LEBIH kuat):
{_track_record_block(ticker)}

Tugasmu (sebagai ANALYST):
1. Identifikasi katalis & pola yang berlaku (rujuk aturan yang relevan, termasuk makro/MSCI/kalender).
2. Susun tesis: argumen bullish vs bearish.
3. Skenario ke depan: BASE / BULL / BEAR singkat + pemicunya.
4. Sebut risiko utama.
5. Beri kecenderungan arah (UP/DOWN/FLAT) + keyakinan kasar yang TERKALIBRASI (jangan overconfident).
6. Nilai cocok JANGKA PENDEK (trading 1-10 hari, basis teknikal/momentum) atau JANGKA PANJANG (hold mingguan-bulanan, basis fundamental kuat: valuasi murah, ROE tinggi, growth, dividen).
Saham ini lolos screening teknikal — tugasmu MEMVERIFIKASI, bukan menerima mentah. Berbasis bukti, jangan mengarang.{tool_hint}"""


def trader_user_prompt(ticker: str, quote: dict, analyst_view: str,
                       position: dict | None, cash: float,
                       horizon: int | None = None) -> str:
    feats = json.loads(quote.get("features_json") or "{}") if quote else {}
    price = quote.get("price") if quote else None
    if position:
        pl = (price / position["avg_price"] - 1) * 100 if price else 0.0
        try:
            days = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(position["opened_at"])).days
        except Exception:  # noqa: BLE001
            days = 0
        pos_txt = (f"KAMU PEGANG posisi ini: {position['qty']:.0f} lembar @ avg "
                   f"{position['avg_price']}, P/L {pl:+.1f}%, sudah {days} hari.\n"
                   f"PUTUSKAN EXIT: HOLD jika masih ada HARAPAN naik (berita/faktor positif) "
                   f"& risiko terkendali; SELL (take-profit) jika untung cukup/target tercapai; "
                   f"SELL (stop-loss) jika tesis rusak / rugi membesar / dipegang kelamaan "
                   f"(tahan terlalu lama = risiko).")
    else:
        pos_txt = "TIDAK punya posisi saham ini"
    return f"""Kamu TRADER pengambil keputusan akhir. Tinjau (dan boleh kritik) analisis di bawah, lalu putuskan.

SAHAM: {ticker} | harga sekarang: {price} | kas tersedia: Rp{cash:,.0f}
{pos_txt}
REKAM JEJAK PREDIKSI DI SAHAM INI: {_track_record_block(ticker)}
FITUR TEKNIKAL: {json.dumps(feats, ensure_ascii=False)}

ANALISIS DARI ANALYST:
\"\"\"{analyst_view[:3500]}\"\"\"

Keluarkan KEPUTUSAN HANYA dalam format JSON valid (tanpa teks lain di luar JSON):
{{
  "ticker": "{ticker}",
  "direction": "UP" | "DOWN" | "FLAT",
  "probability": <0-100, keyakinan arah>,
  "horizon_days": <1-10>,
  "expected_pct": <perkiraan % perubahan harga, boleh negatif>,
  "target_price": <harga target>,
  "action": "BUY" | "SELL" | "HOLD",
  "size_pct": <0-15, % modal untuk dialokasikan jika BUY>,
  "key_factors": ["faktor + BUKTI (berita/data/teknikal/fundamental/MSCI)"],
  "critique": "kritik atas analisis: apa yang kamu SETUJUI dan apa yang kamu TOLAK + alasannya",
  "reasoning": "alasan keputusan, ringkas",
  "term": "pendek" | "panjang" | "keduanya",
  "agree": true|false
}}
"term": "pendek"=trading 1-10 hari (teknikal/momentum), "panjang"=hold mingguan-bulanan (fundamental kuat).
"agree"=true jika sudah SEPAKAT dgn arah & argumen analis; false jika masih ragu (analis membantah lagi).
INI KONFIRMASI ULANG setelah screening teknikal — JANGAN konfirmasi BUY hanya karena sinyal teknikal; pastikan bukti lain (fundamental/berita/arus asing/MSCI) mendukung.
Aturan: BUY hanya jika direction=UP & probability>=60 & expected_pct > ~0.5% (biaya). SELL jika punya posisi DAN (direction=DOWN ATAU sudah waktunya take-profit/stop-loss/kelamaan). Selain itu HOLD.{f'''
WAJIB: prediksi untuk horizon {horizon} hari bursa — set horizon_days={horizon}; expected_pct & target_price HARUS realistis untuk jendela {horizon} hari (jendela pendek = target lebih kecil & keyakinan lebih rendah).''' if horizon else ''}"""


def analyst_rebuttal_system() -> str:
    """System RAMPING untuk putaran bantahan (~120 token vs ~2900 knowledge penuh).
    Analis SUDAH menyusun tesis dengan knowledge penuh di putaran 1 — bantahan cuma butuh
    disiplin argumen, bukan rulebook ulang. Hemat ~2900 tok tiap debat putaran 2+."""
    return ("Kamu ANALYST saham IDX yang disiplin & terkalibrasi. Tanggapi kritik TRADER atas "
            "tesismu: pertahankan poin yang didukung bukti, REVISI yang lemah (mengaku salah "
            "lebih baik daripada ngotot), dan dasarkan semua pada data yang sudah ada di tesis "
            "(teknikal/fundamental/berita/arus asing/makro) — JANGAN mengarang data baru. "
            "Jaga keyakinan tetap rendah hati (overconfidence = musuh utama). Balas ringkas.")


def analyst_rebuttal_prompt(ticker: str, prior_view: str, trader_critique: str) -> str:
    return f"""Kamu ANALYST. TRADER mengkritik tesismu untuk {ticker}. Tanggapi: pertahankan
poin yang benar, REVISI yang lemah, dan perkuat dengan bukti (teknikal/fundamental/berita/MSCI/makro).
Akhiri dengan arah final (UP/DOWN/FLAT) + estimasi % perubahan + horizon, sejujur & sekalibrasi mungkin.

TESIS AWALMU:
\"\"\"{prior_view[:2500]}\"\"\"

KRITIK TRADER:
\"\"\"{trader_critique[:1500]}\"\"\"

Balas ringkas & tajam (bukan mengulang)."""
