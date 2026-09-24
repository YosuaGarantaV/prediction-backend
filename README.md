# Sistem Multi-Agen Saham IDX

Kode sumber skripsi **"Implementasi Sistem Multi-Agen Berbasis Large Language Model untuk
Simulasi Perdagangan Saham di Bursa Efek Indonesia"** — Yosua Aldrin Garanta (23.11.5574),
S1 Informatika, Fakultas Ilmu Komputer, Universitas AMIKOM Yogyakarta, 2026.

Sistem multi-agen **berarsitektur terpusat**: satu koordinator mengatur **enam peran agen**
berbasis large language model, dan **tujuh belas gerbang deterministik** (lima belas aktif pada
akhir periode pengamatan) menegakkan keputusan yang tidak diserahkan kepada model. Sistem
berjalan 24 jam, membaca harga, berita, makro, dan fundamental emiten, memprediksi arah +
keyakinan + horizon dalam hari bursa, menjalankan **simulasi perdagangan** dengan uang
virtual, lalu menilai sendiri setiap prediksi terhadap harga yang terjadi.

> **Bukan nasihat keuangan.** Seluruh transaksi memakai uang virtual. Prediksi bersifat
> probabilistik, dan naskah menyimpulkan keunggulan jalur multi-agen atas mesin heuristik
> **belum cukup bukti** (Tabel 4.8).

---

## Hubungan repo ini dengan naskah

Naskah mengukur keadaan kode **15 Juli sampai 6 September 2026** (periode pengamatan). Repo ini
adalah keadaan kode sesudahnya, jadi sebagian kecil perilakunya sudah bergeser dari yang
tertulis di Bab III. Semua pergeseran yang diketahui:

| pergeseran | berkas | bagian naskah yang terdampak |
|---|---|---|
| Jual karena prediksi ulang FLAT diveto; beli ulang ticker yang sama di hari yang sama diveto (8 Sep) | `app/trading/paper.py` | gerbang tambahan di luar 17 gerbang Tabel 3.5 |
| Listing baru wajib punya 30 sesi riwayat sebelum masuk universe (8 Sep) | `app/repo.py` `liquid_tickers` | gerbang 1 |
| Vonis lengan (gerbang 6 dan 17) memakai base rate yang diukur di baris yang sedang dinilai; tabel statis jadi cadangan (8 Sep) | `app/eval.py` `measured_baseline`, `app/agents/skill.py` | saran 5.2 butir 5 |
| Target harga ditarik ke fraksi harga IDX (9 Sep) | `app/tick.py` | Gambar 3.3 |
| Gaya `invest` 10 → 20 hari bursa; gaya `kuartal`/`semester`/`tahunan` (60/120/250) lewat `FOCUS_TICKERS` (10 Sep) | `config.FOCUS_STYLES` | batasan masalah butir 2 (horizon 1–10 hari bursa) |
| Putaran tool analis ke-2 dst. memakai system prompt ramping (10 Sep) | `app/agents/prompts.py` | Tabel 3.4 |
| Pagu token harian `DAILY_TOKEN_BUDGET` (10 Sep) | `app/agents/budget.py` | — |
| Pin primer agen sentimen kini `nara_free/glm-5.3-flash-free`; Tabel 3.1 mencatat `minimax-m3-free`, yang kini membalas 404 (tanggal pergantian tidak tercatat di kode) | `config.SENTIMENT_CHAIN` | Tabel 3.1 |

Satu hal yang berubah **di dalam** periode dan perlu dibaca bersama persamaan (2.1): sejak
31 Agustus 2026 suku berita pada skor komposit diskalakan `0,80 / 5` per poin sentimen
(sumbangan maksimum ±0,80), bukan `0,80` per poin. Lihat catatan di `app/agents/heuristic.py`.

---

## Arsitektur (Gambar 3.2)

```
lapisan data (app/data/*)  harga, berita RSS, makro global, arus asing, fundamental
        │
mesin heuristik            skor komposit ke seluruh universe likuid (gerbang 1–6)
        │   2 kandidat teratas di luar jam bursa, 4 saat bursa buka → rantai agen
        │   25 peringkat teratas → dicatat langsung oleh mesin heuristik
        ▼
koordinator (LangGraph StateGraph, app/agents/graph.py — satu state bersama)
   analis ─► sentimen ─► trader/CTO ─► [dewan: teknikal · kontrarian · sentimen] ─► [bantahan ⟲]
                                                          │
                                         risk agent (gerbang 7–13, 0 token)
        │
gerbang pencatatan 14–17 (app/agents/orchestrator.py) → tabel predictions → paper trading
        │
evaluasi otonom: prediksi jatuh tempo dinilai dalam hari bursa → pelajaran → evaluator
        → maks 10 aturan disuntikkan ke prompt siklus berikutnya
```

Agen mengembalikan hasilnya kepada koordinator tanpa memanggil agen lain; urutan pemanggilan,
isi state, dan titik kegagalan tercatat di satu tempat (`agent_logs`, `conversations`). Bila
graf gagal, `orchestrator._decide_for` menjalankan jalur manual yang sama. Dengan
`LLM_STRICT=true` (sejak 2 Sep 2026) siklus yang seluruh penyedianya gagal dilewati tanpa
prediksi, supaya keputusan heuristik tidak tercatat sebagai keputusan agen.

### Peran agen (Tabel 3.1 dan 3.4)

| peran | modul | pin primer di commit ini | tugas |
|---|---|---|---|
| Analis | `app/agents/analyst.py` | `nara / qwen3.7-flash` | tesis arah + risiko; boleh menarik data lewat tool call |
| Trader/CTO | `app/agents/trader.py` | `mistral / mistral-medium-latest` | menyanggah analis, menetapkan arah, keyakinan, horizon, target, aksi |
| Dewan teknikal | `app/agents/council.py` | `openrouter / minimax/minimax-m3:free` | satu suara arah dari price action |
| Dewan kontrarian | `app/agents/council.py` | `nara_smart / gpt-5.6-luna` | mencari alasan tesis analis keliru |
| Sentimen | `app/agents/sentiment.py` | `nara_free / glm-5.3-flash-free` | lapis leksikon selalu jalan; LLM hanya bila ada berita < 12 jam; hasilnya jadi kursi sentimen dewan |
| Evaluator | `app/agents/knowledge.py` | `gemini / gemini-flash-latest` | menyuling hasil + pelajaran 7 hari jadi maks 10 aturan |
| Risk agent + mesin heuristik | `app/agents/risk.py`, `app/agents/heuristic.py` | tanpa LLM | gerbang risiko dan skor komposit |

Rantai cadangan tiap peran ada di `config.py` (`ANALYST_CHAIN`, `TRADER_CTO_CHAIN`,
`COUNCIL_MEMBERS`, `SENTIMENT_CHAIN`, `EVALUATOR_CHAIN`), lengkap dengan riwayat pengukuran
yang melatari pergantian 24 Agustus dan 2 September. Naskah system prompt (Lampiran 3) ada di
`app/agents/prompts.py` dan `app/agents/council.py`. Mengisi `ANTHROPIC_API_KEY` menaruh
`claude-fable-5` di depan semua rantai (`USE_FABLE5`), jadi kosongkan untuk susunan naskah.

### Gerbang deterministik (Tabel 3.5)

| no | gerbang | ambang | letak kode |
|---|---|---|---|
| 1 | likuiditas universe | ≥ Rp1 miliar/hari pada ≥ 50% sesi 30 hari | `repo.liquid_tickers` (`MIN_TURNOVER`, `LIQUID_WINDOW_DAYS`, `MIN_LIQUID_FRAC`) |
| 2 | rezim makro | \|kecenderungan\| ≥ 1,5 → skor ± 0,25 × kecenderungan | `heuristic.decide`, `data/premarket.global_brief` |
| 3 | keselarasan model | arah skor ≠ arah regresi logistik → FLAT 53% | `heuristic.decide` (tak berpengaruh sejak 13 Agt) |
| 4 | kelayakan model | AUC ROC uji ≥ 0,52 | `model.is_usable` (`MIN_USABLE_AUC`) |
| 5 | keyakinan saham tipis | turnover < Rp1 miliar → keyakinan ≤ 65% | `heuristic.decide` |
| 6 | program pembelian | base rate UP + 10 pp (16 pp risk-off), ekspektasi > 0,6%, lengan UP terbukti di atas base rate | `heuristic.decide`, `eval.edge_floor` (`MIN_EDGE_PP`) |
| 7 | ARA dan ARB | ±20% | `paper.pre_trade_checks` |
| 8 | edge tipis | ekspektasi < 2,0% → VETO beli | `paper.pre_trade_checks` (`MIN_EDGE_PCT`) |
| 9 | anti-churn | dijual < 6 jam lalu → VETO beli | `paper.pre_trade_checks` (`REENTRY_COOLDOWN_HOURS`) |
| 10 | falling knife | −12%/3 hari + MACD < 0 → VETO beli | `paper.pre_trade_checks` |
| 11 | valuasi ekstrem | PER > 100 atau PBV > 12 → VETO beli | `paper.pre_trade_checks` |
| 12 | tekanan asing | dicabut 31 Agt 2026 (alasannya tercatat di kode) | `paper.pre_trade_checks` |
| 13 | kapasitas portofolio | 8 posisi / 15% modal → VETO atau RESIZE | `paper.pre_trade_checks` (`MAX_POSITIONS`, `MAX_ALLOC_PER_TRADE`) |
| 14 | rezim arah turun | DOWN < 66% saat risk-on atau pembalikan lokal → FLAT | `orchestrator._apply_regime_gate` |
| 15 | tren indeks | arah melawan IHSG vs SMA5 tanpa berita emiten \|impact\| ≥ 2 dalam 48 jam → FLAT | `orchestrator._trend_agreement_gate` |
| 16 | bukti arah naik | UP tanpa pendukung non-teknikal → FLAT | `orchestrator._evidence_gate` |
| 17 | lengan rugi | arah h ≥ 2 terbukti di bawah base rate → FLAT | `orchestrator._proven_loss_gate` |

Aturan keluar posisi: jual paksa −10%, ambil untung +20%, maksimum 15 hari
(`paper.check_exits`). Gerbang 7–13 dijalankan node risk agent sebelum pencatatan, lalu
diperiksa ulang saat eksekusi.

### Skor komposit (persamaan 2.1–2.2, Tabel 3.3)

`heuristic.score_signals` menjumlahkan dua belas aturan teknikal + suku berita;
`heuristic.score_to_view` memetakan skor ke arah (ambang ±1,0), keyakinan mentah
`klip(50 + 9·|S|; 35; 85)`, dan horizon 3 hari. Target dibatasi 1,5 × σ20 × √h, lalu ditarik
ke fraksi harga IDX.

---

## Menghasilkan ulang angka Bab IV (Tabel 3.6, Lampiran 2 dan 5)

Basis data periode pengamatan (`prediction_v4.db`) tidak ikut repo (lihat bagian berikut).
Letakkan salinannya di `data/`, lalu:

```bash
# Tabel 4.3 / Lampiran 5 — base rate cocok-tanggal + block bootstrap per tanggal
python basecheck.py data/prediction_v4.db --from 2026-07-15 --until 2026-08-30 --cut 2026-08-30
python basecheck.py data/prediction_v4.db --from 2026-07-15 --until 2026-09-06 --cut 2026-09-06
python basecheck.py --self-check      # sinyal sempurna wajib lulus, sinyal acak wajib gagal
python -m app.eval --self-check       # pengukur base rate yang dipakai gerbang produksi
```

Keluaran `basecheck.py` bertata letak sama dengan Lampiran 5 (`_g` = rata-rata per sinyal,
`_t` = rata-rata per tanggal, `d_t` = selisih per tanggal). Tambah `--statis` untuk
pembanding tabel statis `BASELINE_WINRATE`.

Besaran Tabel 3.6 dan letaknya di kode:

| butir | nilai | letak |
|---|---|---|
| ambang menang | gerak > 0,5% ke arah yang diklaim | `eval.WIN_BAND_PCT`, `basecheck.THR` |
| ambang FLAT | \|gerak\| ≤ 1,75% | `orchestrator.FLAT_BAND_PCT` |
| uji signifikansi | block bootstrap 3.000 ulangan, blok 5 tanggal, benih 0 | `eval.block_bootstrap` |
| biaya transaksi | 0,15% beli, 0,25% jual | `config.FEE_BUY`, `config.FEE_SELL` |
| modal simulasi | Rp100.000.000, lot 100 lembar | `config.START_CASH`, `app/trading/paper.py` |
| horizon | hari bursa, kalender libur nasional | `app/market_calendar.py` |

`v4stats.py`, `daya_uji.py`, `daya_h1.py`, dan `lampiran5.py` (statistik deskriptif, daya uji,
dan pencetak Lampiran 5) tinggal di folder naskah `../Skripsi/`, bukan di repo ini.

---

## Isi repo ini

Ini paket **backend saja**: FastAPI + scheduler + lapisan agen + lapisan data + test.
Dashboard Next.js terpisah dan tidak disertakan; `app/web/*.html` yang dilayani FastAPI
langsung tetap ada, jadi aplikasi bisa dijalankan tanpa langkah build apa pun.

Yang sengaja TIDAK ikut, dan alasannya:

| tidak disertakan | alasan |
|---|---|
| `.env`, `.ssh/` | kredensial. Pakai `.env.example` sebagai titik mulai (nilainya = susunan naskah) |
| `data/` (DB, arsip harga, log) | data pasar dan riwayat paper-trading milik pemasangan tertentu |
| `frontend/` | UI Next.js terpisah, di luar cakupan repo backend |
| jurnal operasi internal | berisi alamat server, perintah rollback, dan catatan keputusan |

```
run.py                      → start DB + scheduler 24 jam + web dashboard
serve.py                    → supervisor: jalankan run.py dan restart otomatis bila mati
config.py                   → konfigurasi (rantai penyedia per peran, ambang gerbang, interval)
basecheck.py                → base rate cocok-tanggal (Tabel 4.3, Lampiran 5)
app/
 ├ db.py / repo.py          → SQLite: 16 tabel pada Lampiran 1 + tabel shadow
 ├ data/                    → harga (yfinance .JK), berita RSS, makro, arus asing, fundamental
 ├ agents/
 │   ├ graph.py             → KOORDINATOR: StateGraph LangGraph, state bersama
 │   ├ orchestrator.py      → siklus scan → rantai agen → gerbang 14–17 → pencatatan + evaluasi
 │   ├ analyst.py / trader.py / council.py / sentiment.py → peran LLM
 │   ├ risk.py              → risk agent (gerbang 7–13 dari paper.pre_trade_checks)
 │   ├ heuristic.py         → mesin heuristik: skor komposit, gerbang 2–6
 │   ├ llm.py               → klien OpenAI-compatible, rantai cadangan, pemutus sirkuit
 │   ├ knowledge.py / prompts.py → basis pengetahuan, system prompt, evaluator
 │   ├ skill.py             → kalibrasi keyakinan + vonis lengan terhadap base rate
 │   └ shadow.py            → kelompok uji kandidat identik (subbab 3.2.8)
 ├ eval.py                  → base rate, block bootstrap, daya uji
 ├ trading/paper.py         → simulasi perdagangan (lot 100, fee IDX, gerbang 7–13)
 ├ scheduler.py             → job berkala (harga/berita/makro/arus asing/siklus agen/evaluasi)
 └ web/*.html               → antarmuka (Gambar 4.1)
tools/                      → audit penyedia, uji gerbang, analisis galat
tests/                      → pytest
```

### Menjalankan

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # isi AUTH_* dan minimal satu kunci provider LLM
python run.py                 # http://localhost:8800
```

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

Naskah menjalankan layanan pada Python 3.12 (Ubuntu 24.04, aarch64); test suite juga lulus
di Python 3.11. Untuk tes tanpa kuota API, set `USE_LLM=false`: sistem memakai mesin
heuristik saja (tetap menghasilkan prediksi dan transaksi simulasi).

### Test

```bash
pytest -q          # test ada di tests/, jalan dari root maupun folder lain
```

Klon bersih memberi **237 passed, 5 skipped**. Lima yang di-skip butuh data yang tidak ikut
repo (arsip arus asing, tabel harga terisi, kunci provider); tiap skip menyebutkan perintah
pengisinya. Nol kegagalan adalah kondisi normal — kalau ada yang merah, itu memang bug.
Di server yang sedang berjalan pakai `bash tools/livetest.sh` supaya test tidak menulis ke DB
produksi.

### Sebelum percaya angkanya

Sistem ini mengukur dirinya sendiri dan menyimpan hasilnya, termasuk saat hasilnya jelek.

| perintah | menjawab |
|---|---|
| `python tools/provider_audit.py` | penyedia mana yang benar-benar hidup hari ini |
| `curl localhost:8800/api/health` | sisa pagu token hari ini (reset 00:00 WIB) |
| `python tools/error_gap.py --bets-only` | di emiten dan kondisi mana edge hilang |
| `python tools/gate_trial.py --bets-only` | apakah kandidat gerbang baru benar-benar layak |
| `python basecheck.py` | win rate vs base rate yang dicocokkan per tanggal |

Pin model membusuk diam-diam (model ditarik, kredit habis, kunci dicabut); gejalanya bukan
crash melainkan kursi dewan yang diam dan rantai cadangan yang mengambil alih (subbab 4.5).
Jalankan audit penyedia tiap dua minggu.

---

## Konfigurasi penting (`.env`)

| variabel | arti |
|---|---|
| `USE_LLM` | `true` = rantai agen, `false` = mesin heuristik saja |
| `LLM_STRICT` | `true` = siklus dilewati bila semua penyedia gagal (mode ketat, sejak 2 Sep 2026) |
| `USE_LANGGRAPH` | `true` = koordinator StateGraph (bawaan); `false` = jalur manual |
| `AGENT_CYCLE_MIN` | interval siklus agen (menit, bawaan 12) |
| `CANDIDATES_PER_CYCLE` | kandidat ke rantai agen di luar jam bursa (bawaan 2; ×2 saat bursa buka) |
| `START_CASH` | modal awal simulasi (Rp) |
| `PRICE_REFRESH_MIN` / `NEWS_REFRESH_MIN` | interval penyegaran harga / berita saat bursa buka |
| `SHADOW_ARMS` / `SHADOW_LLM` | kelompok uji kandidat identik dan model tunggal yang dipatok |
| `FOCUS_TICKERS` | saham prioritas + gaya: `BBCA:invest,BBRI:swing,ANTM:scalp` |

Watchlist, sumber berita, dan penggerak makro diatur di `config.py`. Saat pasar tutup
(malam/akhir pekan) job harga/arus/berita/anomali otomatis diperlambat atau dilewati
(`scheduler._hemat`).

UI multi-halaman: `/` dashboard, `/p/saham?t=KODE` detail saham (fundamental, riwayat
prediksi, transkrip debat agen, berita emiten), `/p/prediksi` semua prediksi + akurasi,
`/p/portofolio` posisi/transaksi, `/p/sistem` kesehatan/token/log/pelajaran.
