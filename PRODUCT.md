# Product

## Register

product

## Users

Dua tingkat pengguna, satu orang yang sama pada waktu berbeda:

1. **Pemula / pengguna baru** — membuka aplikasi tanpa pemahaman trading atau UI manajemen.
   Tidak tahu apa itu RSI, horizon, win-rate, atau paper trading. Pertanyaannya:
   "Aplikasi ini apa? Saya harus mulai dari mana? Saham mana yang diprediksi naik, dan kenapa?"
2. **Pemilik/operator (Yosua)** — memantau kinerja engine harian, tuning aturan, membaca
   laporan, mengecek token & kesehatan sistem. Butuh data padat, tapi di halamannya sendiri.

Konteks pemakaian: laptop Windows, browser, cek pagi sebelum bursa buka dan sore setelah tutup.

## Product Purpose

Mesin prediksi saham IDX multi-agent (LLM analyst + trader + dewan) yang berjalan 24 jam:
scan seluruh bursa, prediksi arah + keyakinan + horizon, dan paper-trading untuk mengukur diri.
Sukses = pengguna baru paham dalam 30 detik apa yang aplikasi katakan hari ini (sinyal teratas +
alasannya), dan operator bisa menelusuri detail sedalam apa pun tanpa menyesaki halaman utama.

Bukan nasihat keuangan; uang palsu; prediksi probabilistik — kejujuran metrik adalah fitur.

## Brand Personality

Analitik, jujur, tenang. Terminal keuangan yang menjelaskan dirinya sendiri — bukan cockpit
pesawat. Tanpa emoji, tanpa hype, angka selalu dengan konteks ("dari 2.325 prediksi selesai").
Bahasa Indonesia polos; istilah teknis selalu diberi terjemahan sekali-baca.

## Anti-references

- Dashboard "semua-panel-dalam-satu-layar" (Grafana default, admin template): tembok data tanpa
  hierarki, pengguna baru tersesat.
- Jargon telanjang: "hot-set", "OOS", "lean", "heuristik" tanpa penjelasan di UI.
- Slate/cyan Tailwind default dan hero-metric SaaS.
- Onboarding modal yang memblokir; tur paksa.

## Design Principles

1. **Sinyal dulu, angka kemudian** — hal pertama di layar adalah kesimpulan berbahasa manusia
   ("BBRI diprediksi NAIK +2% dalam 5 hari, karena…"), bukan KPI.
2. **Tiap panel menjelaskan dirinya** — satu baris keterangan polos di bawah tiap judul; istilah
   teknis dapat tooltip atau glosarium.
3. **Kedalaman lewat halaman, bukan tumpukan** — dashboard = ringkasan; detail per saham,
   prediksi, portofolio, sistem hidup di halamannya sendiri.
4. **Jujur secara metrik** — tampilkan n, tampilkan kalah, jangan sembunyikan kekalahan di balik
   agregat.
5. **Tanpa pekerjaan wajib** — engine jalan sendiri; onboarding memberi tahu, tidak menyuruh.

## Accessibility & Inclusion

- Kontras teks ≥4.5:1 pada tema gelap (muted #97A6A4 di panel #161B1D ≈ 6:1).
- Arah tidak pernah dikomunikasikan warna saja — selalu dengan panah/label (▲ NAIK / ▼ TURUN).
- `prefers-reduced-motion` dihormati; tidak ada animasi dekoratif.
- Bahasa Indonesia untuk semua UI copy.
