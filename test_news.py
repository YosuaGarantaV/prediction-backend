"""Cek kualitas berita: sentimen sadar-konteks, filter clickbait, dedup (python test_news.py)."""
from app import repo
from app.data import news


def test_sentiment_context():
    # "inflasi turun" BULLISH walau mengandung kata negatif "turun"
    s, m = news._sentiment("Inflasi turun, rupiah menguat terhadap dolar")
    assert s == "positive" and m > 0, (s, m)
    # clickbait 1-kata tak boleh dipaksa magnitudo 3
    _, m = news._sentiment("Perang harga diskon, saham promo melambung")
    assert m > -3, m
    # krisis NYATA → magnitudo penuh 3
    s, m = news._sentiment("Invasi militer pecah, bursa anjlok krisis")
    assert s == "negative" and m == -3, (s, m)
    print("ok")


def test_noise():
    assert news._is_noise("Rekomendasi Saham Hari Ini, Auto Cuan!")
    assert news._is_noise("Bocoran saham gocap pasti untung")
    assert not news._is_noise("BI tahan suku bunga acuan di 5,75%")
    print("ok")


def test_dedup():
    rows = [{"title": "IHSG ditutup menguat 1%"},
            {"title": "IHSG ditutup menguat 1%!"},   # kembar (beda tanda baca)
            {"title": "Rupiah melemah ke 16.300"}]
    assert len(repo._dedup_news(rows)) == 2
    print("ok")


def test_market_wide():
    # Berita makro pasar-luas → harus terdeteksi (akan dinaikkan ke scope=global → sampai ke semua analis)
    assert news._is_market_wide("MSCI Putuskan RI Tetap Emerging Market")
    assert news._is_market_wide("BI rate ditahan, rupiah menguat")
    # Berita 1 emiten murni → BUKAN pasar-luas (tetap lokal)
    assert not news._is_market_wide("Laba bersih ICBP naik 12% kuartal ini")
    print("ok")


def test_relevant():
    # Off-topic (olahraga/hiburan/gaya hidup/perusahaan asing) → dibuang
    assert not news._is_relevant("Pratinjau Ceko vs Meksiko: El Tri buru poin", [], 0)
    assert not news._is_relevant("Prime Video tayangkan prekuel Legally Blonde", [], 0)
    assert not news._is_relevant("Cathay Pacific shares rise 3% on passenger growth", [], 0)
    # Relevan pasar → disimpan
    assert news._is_relevant("Merdeka tetapkan dividen pertama", ["MDKA"], 1)   # emiten ter-tag
    assert news._is_relevant("Awas perang AS-Iran soal nuklir", [], -3)          # impact kuat
    assert news._is_relevant("Nilai tukar rupiah hari ini menguat", [], 0)       # term pasar
    print("ok")


def test_tag_wordboundary():
    # substring nyasar TAK boleh nge-tag (kasus nyata dari feed)
    assert news._tag_tickers("Pemerintah buka suara soal korupsi") == []          # 'buka'≠BUKA
    assert news._tag_tickers("Corporate earnings moderate, prospek membaik") == []  # 'rate' terbenam ≠ bank
    assert news._tag_tickers("Program Laga Perubahan NasDem soal politik") == []
    # tag SAH tetap jalan: kode HURUF BESAR utuh + nama perusahaan
    assert "BUKA" in news._tag_tickers("Bukalapak (BUKA) cetak rugi bersih")
    assert "BBCA" in news._tag_tickers("BBCA cetak laba, saham menguat")
    print("ok")


def test_sentiment_wordboundary():
    # 'war' (perang, Inggris) TAK boleh memicu negatif di 'Warisan'/'warga'
    s, m = news._sentiment("Surya Paloh: Kraton Majapahit Warisan Berharga")
    assert s == "neutral" and m == 0, (s, m)
    # off-topic budaya/politik tanpa tag & tanpa term pasar → dibuang
    assert not news._is_relevant("Surya Paloh: Kraton Majapahit Warisan Berharga",
                                 news._tag_tickers("Surya Paloh: Kraton Majapahit Warisan Berharga"), 0)
    print("ok")


def test_rupiah_bare_not_a_signal():
    """Audit 2026-07-13: 'rupiah' sbg SATUAN mata uang (bukan sinyal kurs) tak boleh
    meloloskan berita non-saham atau salah-tag bank. Kasus nyata: tarif TransJakarta
    "200 ribu rupiah/bulan" ter-tag BBCA/BBRI/BMRI + lolos relevan sbg global."""
    blob = ("Dewan Transportasi Usul Tarif Langganan TransJakarta Rp200 Ribu per Bulan. "
            "Dewan Transportasi Kota Jakarta mengusulkan skema tarif baru TransJakarta, "
            "paket langganan 200 ribu rupiah per bulan, tarif reguler naik jadi 5 ribu rupiah.")
    assert news._tag_tickers(blob) == [], news._tag_tickers(blob)
    assert not news._is_market_wide(blob)
    sentiment, impact = news._sentiment(blob)
    assert not news._is_relevant(blob, [], impact), (sentiment, impact)
    # Kurs SUNGGUHAN (frasa spesifik) tetap terdeteksi & tetap tag bank
    assert news._is_market_wide("Nilai tukar rupiah hari ini menguat tajam")
    assert "BBCA" in news._tag_tickers("Rupiah menguat, saham perbankan ikut naik")
    print("ok")


def test_food_price_not_bullish():
    """Audit 2026-07-13: 'Harga Pangan Naik' salah dpt sentimen POSITIF (bare kata 'naik')
    — inflasi pangan = tekanan daya beli, bukan bullish saham."""
    s, m = news._sentiment("Harga Pangan Naik di Semarang, Timun Jadi Komoditas Termahal")
    assert s != "positive", (s, m)
    print("ok")


def test_policy_figure_needs_context():
    """Audit 2026-07-13: nama tokoh SENDIRIAN (kunjungan pribadi/gosip keluarga) bukan sinyal
    pasar — kasus nyata 'Anak Hashim Keponakan Prabowo ke IKN' harus dibuang. Prabowo +
    ISU KEBIJAKAN (gaji guru/kopdes/MBG dst, kasus lama) tetap harus lolos (no regression)."""
    gossip = "Anak Hashim Keponakan Prabowo Datang ke IKN, Beri Respons Tak Terduga"
    assert not news._is_market_wide(gossip)
    assert not news._is_relevant(gossip, news._tag_tickers(gossip), 0)
    policy = "Prabowo Singgung Gaji Guru, Kopdes, dan MBG dalam Pidato Kenegaraan"
    assert news._is_market_wide(policy)
    assert news._is_relevant(policy, [], 0)
    print("ok")


if __name__ == "__main__":
    test_sentiment_context()
    test_noise()
    test_dedup()
    test_market_wide()
    test_relevant()
    test_tag_wordboundary()
    test_sentiment_wordboundary()
    test_rupiah_bare_not_a_signal()
    test_food_price_not_bullish()
    test_policy_figure_needs_context()
