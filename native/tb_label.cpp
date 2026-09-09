// Hot path triple-barrier labeling — C++ (loop bar x horizon lintas semua ticker).
// Dikompilasi via native/build_tb.py (cffi). Fallback numpy otomatis di app/label.py:
// kalau modul ini tak terkompilasi, hasil TETAP benar lewat _triple_barrier_numpy.
//
// Kontrak (identik dgn versi numpy): untuk tiap bar i in [0, n-max_h):
//   out[i] = 1.0  bila harga menyentuh +up SEBELUM -dn dalam max_h bar,
//          = 0.0  bila menyentuh -dn dulu, ATAU waktu habis & harga akhir <= entry,
//          = 1.0  bila waktu habis & harga akhir > entry.
// Ekor (i >= n-max_h) dan entry<=0 => sentinel -2.0 (di-Python-kan jadi nan).

extern "C" void triple_barrier_c(const double* closes, int n,
                                 double up, double dn, int max_h, double* out) {
    for (int i = 0; i < n; ++i) out[i] = -2.0;          // default: tak terdefinisi
    for (int i = 0; i < n - max_h; ++i) {
        double entry = closes[i];
        if (entry <= 0.0) { out[i] = -2.0; continue; }
        double lab = -1.0;                               // -1 = belum tersentuh
        for (int h = 1; h <= max_h; ++h) {
            double r = closes[i + h] / entry - 1.0;
            if (r >= up) { lab = 1.0; break; }
            if (r <= -dn) { lab = 0.0; break; }
        }
        if (lab < 0.0) lab = (closes[i + max_h] > entry) ? 1.0 : 0.0;  // barrier waktu
        out[i] = lab;
    }
}
