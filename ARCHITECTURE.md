# Arsitektur

Dokumen ini menjelaskan struktur kode, alur data, dan aturan bisnis yang tidak
langsung terlihat dari nama fungsi — banyak di antaranya ditemukan lewat debugging
data produksi asli, jadi dicatat di sini supaya tidak perlu ditemukan ulang.

Untuk cara jalankan/deploy, lihat [README.md](README.md). Untuk konvensi menulis
kode & cara menambah fitur, lihat [CONTRIBUTING.md](CONTRIBUTING.md).

## Peta apps

Django project `config/` membungkus enam app, masing-masing satu tanggung jawab:

| App | Tanggung jawab |
|---|---|
| `apps/core` | Model dasar (`TimeStampedModel`, `money_field`), enum bersama (`Channel`, `MatchStatus`, dll — `apps/core/enums.py`), dan `apps/core/normalize.py`: fungsi murni (tanpa Django/DB, gampang di-unit-test) untuk menormalkan teks keterangan bank/Otomax jadi kunci pencocokan. |
| `apps/catalog` | Data referensi: `Reseller`, `ResellerAlias` (ejaan nama Otomax → satu reseller), `MerchantMap` (merchant ID QRIS → reseller), `ExclusionRule` (aturan pemisahan transaksi non-engine, mis. biaya admin). |
| `apps/ingest` | Parser file mentah per bank/Otomax (`apps/ingest/parsers/*.py`) + `services.py` yang mengubah hasil parse jadi baris `BankMutation`/`OtomaxEntry` di DB. |
| `apps/recon` | Inti bisnis: mesin pencocokan (`engine/`), carry-forward lintas hari (`carry.py`), tutup/buka buku (`close.py`), resolusi manual (`resolve.py`), laporan (`reports.py`), model (`models.py`). |
| `apps/dashboard` | UI (Django template + HTMX/Alpine) — `views/` satu file per halaman, lihat tabel di bawah. |
| `apps/api` | Endpoint REST tipis yang manggil fungsi service yang sama dengan dashboard (upload, jalankan pencocokan, laporan). **Catatan:** endpoint ini saat ini tidak diproteksi login — lihat bagian Keamanan di bawah. |

## Model inti & relasinya

```
ImportBatch (satu file yang diunggah)
  ├─ BankMutation (baris kredit mutasi bank, sudah dinormalisasi)
  └─ OtomaxEntry   (baris export Otomax, bisa negatif — TOPUP_TARTUN / REVERSAL / ADMIN / dll)

Match (satu pasangan bank ↔ otomax, atau grup QRIS)
  ├─ bank_mutation      -> BankMutation (nullable — kosong utk match "tag manual Otomax")
  ├─ otomax_entry       -> OtomaxEntry  (baris utama, nullable — kosong utk "tag manual bank")
  └─ otomax_entries     -> OtomaxEntry  (M2M, dipakai match_type=AGGREGATE — lihat bawah)

Discrepancy (selisih yang belum/sudah diselesaikan)
  ├─ origin_book_date   tanggal buku ASAL, tidak pernah berubah
  └─ resolution_match   -> Match (kalau diselesaikan lewat carry-forward/manual)

Adjustment (penyesuaian bertanggal ke hari yang sudah ditutup — append-only, tidak bisa diedit/dihapus)

ReconDay (snapshot beku per tanggal buku setelah ditutup; locked=True mengunci impor & pencocokan ulang)
```

## Alur pipeline rekonsiliasi

1. **Impor** (`apps/ingest/services.py::import_file`) — parse file mentah, hitung
   `ref_normalized`/`ref_core`/`extracted_tokens` (lewat `apps/core/normalize.py`),
   terapkan `ExclusionRule` aktif (baris yang cocok dipindah ke `ExcludedTransaction`,
   tidak pernah masuk mesin pencocokan), simpan sisanya sebagai `BankMutation`/`OtomaxEntry`
   berstatus `UNMATCHED`. Ditolak kalau tanggal buku sudah `locked`.

2. **`run_match(book_date)`** (`apps/recon/engine/__init__.py`), dipanggil lewat tombol
   "Jalankan Pencocokan" atau `manage.py recon_match`:
   1. **Netting REVERSAL** (`engine/reversal_netting.py`) — jalan **lintas semua tanggal**,
      bukan cuma `book_date` yang diminta. Lihat "Kenapa netting pakai `ref_core`" di bawah.
   2. **Pencocokan per channel** untuk `book_date`: QRIS lewat `engine/qris_match.py`
      (agregat per reseller), sisanya (BRI/BCA/Mandiri) lewat `engine/ref_match.py`
      (per-ref: exact → token inti → fuzzy → fallback nominal Tartun PLC).
   3. **Leftover** (`engine/leftovers.py`) — sisa yang masih terbuka setelah semua di
      atas dicatat sebagai `Discrepancy`.

3. **`carry_forward(book_date)`** (`apps/recon/carry.py`) — coba selesaikan `Discrepancy`
   `OPEN` dari hari-hari **sebelumnya** pakai data `book_date` yang baru masuk. Kalau
   ketemu, posting `Adjustment` bertanggal hari asal (hari asal **tidak** disentuh/diedit).

4. **`close_day(book_date)`** (`apps/recon/close.py`) — bekukan snapshot, kunci hari.
   Ditolak kalau masih ada `AUTO_FUZZY` belum direview atau QRIS belum ke-mapping
   (kecuali `force=True`).

## Aturan bisnis yang tidak jelas dari nama fungsi

**`PENDING_SETTLE` = status "masih terbuka", sama seperti `UNMATCHED`.**
`_OPEN_STATUSES` (`engine/helpers.py`) dipakai di setiap query kandidat pencocokan
(`_open_otomax`, `_net_reversals`, `carry.py::_find_otomax_for`). Kalau nanti menambah
query kandidat baru, pastikan pakai `match_status__in=_OPEN_STATUSES`, **bukan**
`match_status=MatchStatus.UNMATCHED` saja — kalau lupa, entri yang baru saja di-unpair
(status jadi `PENDING_SETTLE`) jadi tidak pernah terlihat lagi oleh mesin manapun.

**Netting REVERSAL pakai `ref_core`, bukan `ref_normalized`.** Otomax kadang menambah
akhiran `"TGL dd/bln/yyyy"` hanya pada baris `REV .../revisian`, tidak pada entri
aslinya — jadi `ref_normalized`-nya (teks persis) bisa beda walau merujuk transaksi
yang sama. `ref_core` (token angka inti) tahan terhadap variasi ini. REV juga bisa
menetralkan REV lain (bukan cuma TOPUP_TARTUN) untuk kasus koreksi berlapis.

**`MATCH_AMOUNT_TOLERANCE`** (`config/settings/base.py`, default `0`) membatasi Pass 3
di `qris_match.py`: nama outlet cocok tapi nominal beda boleh di-auto-match+ditandai
`AMOUNT_DIFF` **hanya** kalau selisihnya di dalam toleransi ini. Di luar itu, kedua
sisi dibiarkan terpisah di antrean manual — jangan auto-link tanpa batas, walau nama
outletnya cocok, karena bisa saja itu dua transaksi berbeda yang kebetulan mirip nama.

**`Match.otomax_entries` (M2M)** menyimpan semua baris Otomax dalam satu grup untuk
`match_type=AGGREGATE` (QRIS multi-baris) — `otomax_entry` (FK tunggal) cuma baris
"utama" untuk tampilan. `unpair_match()` (`resolve.py`) melepas **semua** anggota
`otomax_entries`, bukan cuma `otomax_entry`. Kalau membuat Match AGGREGATE baru di
tempat lain, jangan lupa `match.otomax_entries.set(...)`.

**Day-lock & append-only.** `ReconDay.locked=True` menolak impor baru dan pencocokan
ulang untuk tanggal itu. Selisih yang ketinggalan diselesaikan lewat `Adjustment`
bertanggal hari asal (append-only, tidak bisa diedit — lihat `Adjustment.save()`/
`delete()`), bukan dengan mengedit balik data hari yang sudah ditutup.

## Keamanan — perlu diperhatikan

`apps/api/*` **tidak diproteksi login** (`@csrf_exempt` tanpa `@login_required`) —
siapa pun yang bisa menjangkau server bisa upload file, jalankan pencocokan, atau
tag manual lewat endpoint ini tanpa autentikasi. Ini beda dengan seluruh `apps/dashboard`
yang konsisten pakai `@login_required`. Kalau API ini benar-benar dipakai (bukan cuma
warisan dari draf awal), perlu ditambah autentikasi sebelum dipercaya untuk data
finansial nyata.

## Di mana mencari apa

| Mau ubah... | Lihat |
|---|---|
| Aturan pencocokan per-ref (BRI/BCA/Mandiri) | `apps/recon/engine/ref_match.py` |
| Aturan pencocokan QRIS/agregat | `apps/recon/engine/qris_match.py` |
| Logika netting REV | `apps/recon/engine/reversal_netting.py` |
| Normalisasi teks/ekstraksi ref | `apps/core/normalize.py` |
| Parser file bank/Otomax baru | `apps/ingest/parsers/` (lihat CONTRIBUTING.md) |
| Halaman dashboard tertentu | `apps/dashboard/views/<nama_halaman>.py` |
| Tutup/buka buku, carry-forward | `apps/recon/close.py`, `apps/recon/carry.py` |
| Resolusi manual (tag, pair, unpair) | `apps/recon/resolve.py` |
| Command CLI (`manage.py xxx`) | `apps/recon/management/commands/`, `apps/ingest/management/commands/` |
