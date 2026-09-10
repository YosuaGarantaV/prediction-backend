"""Kompilasi hot path triple-barrier C++ -> native/_tb_native (cffi).

Jalankan:  python native/build_tb.py
Butuh compiler C++: Windows = "MSVC Build Tools" (Desktop C++), Linux/Mac = gcc/clang.
TANPA compiler tak masalah: app/label.py otomatis pakai numpy (hasil identik, lebih lambat).

Verifikasi setelah kompilasi:  python tests/test_triple_barrier.py   (native == numpy)
"""
from cffi import FFI

ffi = FFI()
ffi.cdef("void triple_barrier_c(const double*, int, double, double, int, double*);")
ffi.set_source(
    "native._tb_native",
    '#include <stdint.h>\n'
    'extern "C" void triple_barrier_c(const double*, int, double, double, int, double*);',
    sources=["native/tb_label.cpp"],
    source_extension=".cpp",
)

if __name__ == "__main__":
    ffi.compile(verbose=True)
    print("OK: native/_tb_native terkompilasi. app/label.py kini pakai jalur C++.")
