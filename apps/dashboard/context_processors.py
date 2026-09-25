"""Context processor lintas-halaman: mengingat tanggal buku terakhir yang dipilih.

Beberapa halaman (Daftar Selisih, Kode Reseller, Pengaturan, Reset & Purge, Backup
& Restore) sengaja tidak di-scope ke satu tanggal buku, jadi tidak mengirim
`book_date` ke context. Tanpa ini, begitu operator singgah di salah satu halaman
tersebut, semua link sidebar berikutnya kehilangan `?d=...` dan balik ke hari ini --
padahal operator masih ingin lanjut kerja di tanggal tertentu. `nav_book_date`
dipakai base.html sebagai fallback link sidebar saat `book_date` halaman aktif
kosong.
"""

from __future__ import annotations

from datetime import date


def last_book_date(request):
    session = getattr(request, "session", None)
    if session is None:
        return {"nav_book_date": ""}

    raw = request.GET.get("d")
    if raw:
        try:
            date.fromisoformat(raw)
        except ValueError:
            raw = None
        else:
            session["last_book_date"] = raw

    return {"nav_book_date": raw or session.get("last_book_date", "")}
