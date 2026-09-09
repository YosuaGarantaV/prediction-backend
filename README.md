# Saham IDX Prediction Engine

Web app prediksi saham Bursa Efek Indonesia (IDX). Mesinnya sebuah **pipeline LLM** — dua
peran prompt (Analyst + Trader/CTO) plus dewan pemungut suara opsional — yang bekerja 24 jam:
mengambil data harga live, berita lokal & global, lalu memprediksi arah (naik/turun) +
persentase keyakinan + horizon hari, dan **paper-trading** (uang palsu) untuk mengukur diri.

> Istilah jujur: ini **bukan** sistem multi-agent otonom. Yang mengatur alur adalah kode,
> bukan negosiasi antar-agen.

> **Bukan nasihat keuangan.** Prediksi bersifat probabilistik. Tidak ada jaminan profit.
> Tujuan sistem ini: alat bantu keputusan + eksperimen yang bisa kita tune terus tiap hari.

---

## Isi repo ini

Ini paket **backend saja**: FastAPI + scheduler + lapisan agen + lapisan data + test.
Dashboard Next.js terpisah dan tidak disertakan; `app/web/*.html` yang dilayani FastAPI
langsung tetap ada, jadi aplikasi bisa dijalankan tanpa langkah build apa pun.

Yang sengaja TIDAK ikut, dan alasannya:

| tidak disertakan | alasan |
|---|---|
| `.env`, `.ssh/` | kredensial. Pakai `.env.example` sebagai titik mulai |
| `data/` (DB, arsip harga, log) | data pasar dan riwayat paper-trading milik pemasangan tertentu |
| `frontend/` | UI Next.js terpisah, di luar cakupan repo backend |
| jurnal operasi internal | berisi alamat server, perintah rollback, dan catatan keputusan |

### Menjalankan

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # isi AUTH_* dan minimal satu kunci provider LLM
python run.py                 # http://localhost:8800
```

### Test

```bash
pytest -q
```

Klon bersih memberi **226 passed, 5 skipped**. Lima yang di-skip butuh data yang tidak ikut
repo (arsip arus asing, tabel harga terisi, kunci provider); tiap skip menyebutkan perintah
pengisinya. Nol kegagalan adalah kondisi normal — kalau ada yang merah, itu memang bug.

### Sebelum percaya angkanya

Mesin ini mengukur dirinya sendiri dan menyimpan hasilnya, termasuk saat hasilnya jelek.
Tiga alat bacanya:

| perintah | menjawab |
|---|---|
| `python tools/provider_audit.py` | provider mana yang benar-benar hidup hari ini |
| `python tools/error_gap.py --bets-only` | di emiten dan kondisi mana edge hilang |
| `python tools/gate_trial.py --bets-only` | apakah kandidat gerbang baru benar-benar layak |
| `python basecheck.py` | win-rate vs dasar pasar yang dicocokkan per tanggal |

Pin model LLM membusuk diam-diam (model ditarik, kredit habis, kunci dicabut) dan gejalanya
bukan crash melainkan prediksi yang pelan-pelan jadi hasil fallback. Jalankan audit provider
tiap dua minggu.

---

## Arsitektur

```
run.py                      → start DB + scheduler 24 jam + web dashboard
config.py                   → konfigurasi (watchlist, key, interval)
app/
 ├ db.py / repo.py          → SQLite: harga, berita, prediksi, log, trade, lessons
 ├ data/stocks.py           → harga IDX via yfinance (.JK) + indikator teknikal
 ├ data/news.py             → berita RSS lokal + global, tagging ticker + sentimen
 ├ agents/
 │   ├ llm.py               → client OpenAI-compatible multi-provider + rantai fallback per-peran
 │   ├ knowledge.py         → "pelatihan" = basis aturan saham (system prompt)
 │   ├ prompts.py           → penyusun prompt tiap peran (system + user)
 │   ├ analyst.py           → peran ANALYST (chain: cerebras→github→glm→…) → tesis teks
 │   ├ trader.py            → peran TRADER/CTO (chain: github→minimax→groq→…) → keputusan JSON
 │   ├ council.py           → dewan: N provider vote arah (ensemble), ditally → catatan utk CTO
 │   ├ memo.py              → memoization tesis (reuse bila input tak berubah)
 │   ├ factors.py / skill.py → faktor struktural + kalibrasi probabilitas ke realisasi
 │   ├ heuristic.py         → mesin cadangan tanpa LLM (selalu jalan)
 │   └ orchestrator.py      → SKRIP alur fetch→analisis→(dewan)→debat→keputusan→trade + evaluasi
 ├ trading/paper.py         → engine paper trading (lot 100, fee IDX)
 ├ scheduler.py             → job berkala (harga/berita/siklus agen/evaluasi)
 ├ report.py                → laporan harian (markdown) untuk di-tune Claude
 └ web/dashboard.html       → dashboard UI/UX (dark trading terminal)
```

### Cara pipeline bekerja (per kandidat saham)
1. **ANALYST** membaca teknikal + berita + makro → menyusun tesis bullish/bearish + risiko.
2. **TRADER/CTO** meninjau & **mengkritik** tesis → keputusan terstruktur:
   arah, `probability %`, `horizon_days`, `target_price`, aksi BUY/SELL/HOLD, plus flag `agree`.
3. **Dewan (opsional)** dipanggil **hanya saat trader ragu** (belum `agree` / keyakinan 55–68% /
   mau BUY): N provider memberi vote arah, ditally jadi konsensus, lalu trader memutuskan ulang.
4. **Debat** Analyst↔Trader diulang selama `agree=false`, dibatasi `DEBATE_ROUNDS`.
5. **Paper engine** mengeksekusi dengan disiplin risiko (maks 15%/posisi, maks 8 posisi).
6. **Loop perbaikan diri**: tiap prediksi jatuh tempo dievaluasi (menang/kalah) →
   ditulis jadi *lesson* → diinject ke prompt siklus berikutnya (in-context learning).

Catatan penting: langkah 1–6 adalah **kontrol-flow Python di `orchestrator._decide_for`**, bukan
agen yang memutuskan sendiri kapan saling memanggil. Yang mengatur alur adalah `if`/`while`, bukan
negosiasi antar-agen.

---

## Cara menjalankan (Windows)

```powershell
cd prediction-backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

Buka **http://localhost:8800**

Key provider (NVIDIA/GitHub/Groq/Cerebras/GLM/Gemini/Mistral/OpenRouter) diisi di `.env` —
peran mana pun jalan selama minimal satu key di rantainya ada. Untuk hemat kuota / tes tanpa API,
set `USE_LLM=false` → pipeline pakai mesin heuristik (tetap menghasilkan prediksi & trade).

---

## Tuning harian (alur kerja dengan Claude)

1. Biarkan engine jalan seharian (agen otomatis tiap `AGENT_CYCLE_MIN` menit).
2. Sore/malam buka **Laporan harian** (tombol di dashboard) atau `http://localhost:8800/api/report`.
   Laporan juga otomatis tersimpan di `data/logs/report_YYYYMMDD.md`.
3. Kirim isi laporan itu ke Claude → Claude menganalisis win-rate, prediksi yang meleset,
   dan menyarankan perbaikan aturan di **`app/agents/knowledge.py`**.
4. Ulangi sampai akurasi membaik.

Log lengkap aktivitas agen ada di tabel `agent_logs` (tampil live di dashboard) dan
file `data/logs/engine.log`.

---

## Konfigurasi penting (`.env`)
| Variabel | Arti |
|---|---|
| `USE_LLM` | `true`=pakai pipeline LLM multi-provider, `false`=heuristik lokal |
| `START_CASH` | modal awal paper trading (Rp) |
| `AGENT_CYCLE_MIN` | tiap berapa menit agen menganalisis |
| `TICKERS_PER_CYCLE` | berapa saham per siklus (rotasi watchlist) |
| `PRICE_REFRESH_MIN` / `NEWS_REFRESH_MIN` | interval refresh data |
| `FOCUS_TICKERS` | saham fokus + gaya: `BBCA:invest,BBRI:swing,ANTM:scalp` (scalp=1h, swing=5h, invest=20h bobot fundamental) |

Watchlist saham & sumber berita diatur di `config.py`.

Catatan hemat scraping: saat pasar tutup (malam/akhir pekan) job harga/arus/berita/anomali
otomatis diperlambat atau di-skip (`scheduler._hemat`) — harga IDX beku, refresh penuh sia-sia.

UI multi-halaman: `/` dashboard, `/p/saham?t=KODE` detail saham (fundamental lengkap, riwayat
prediksi, debat agen, berita emiten), `/p/prediksi` semua prediksi + akurasi/faktor/kalibrasi,
`/p/portofolio` posisi/transaksi/ledger, `/p/sistem` kesehatan/token/log/pelajaran.

---
