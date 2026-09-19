# Contributing

Panduan singkat untuk mengembangkan proyek ini. Untuk penjelasan struktur kode &
aturan bisnis, lihat [ARCHITECTURE.md](ARCHITECTURE.md). Untuk cara jalankan/deploy,
lihat [README.md](README.md).

## Setup & menjalankan test

```bash
uv sync
uv run pytest              # seluruh test suite
uv run ruff check apps/    # lint
uv run python manage.py check   # validasi konfigurasi Django (import, URL, dll)
```

Sebelum push perubahan apa pun ke mesin pencocokan/model, jalankan ketiganya —
harus tetap hijau.

## Konvensi

- **Uang selalu `Decimal`**, tidak pernah `float`. Field model pakai `money_field()`
  (`apps/core/models.py`) — `DecimalField(max_digits=15, decimal_places=2)`.
- **Status "masih terbuka" ada dua**: `MatchStatus.UNMATCHED` dan
  `MatchStatus.PENDING_SETTLE` — keduanya berarti "belum ada pasangan aktif". Query
  kandidat pencocokan baru harus pakai `match_status__in=_OPEN_STATUSES`
  (`apps/recon/engine/helpers.py`), bukan `match_status=UNMATCHED` saja — lihat
  ARCHITECTURE.md bagian "Aturan bisnis" untuk alasannya.
- **`MatchStatus.MANUAL`** = sudah diselesaikan manusia lewat "Tag Manual" atau
  "Cocokkan Manual" — final, tidak disentuh mesin otomatis lagi.
- **`MatchStatus.IGNORED`** = dinetralkan mesin (netting REVERSAL) — bukan berarti
  "diabaikan begitu saja", tapi "sudah terbukti bernilai nol, tidak perlu dicocokkan".
- **Migration**: `uv run python manage.py makemigrations <app>` lalu commit file
  migration-nya bersama perubahan model. Jangan edit migration lama yang sudah
  di-deploy.
- **Test baru untuk tiap perbaikan bug**: pola yang dipakai sepanjang proyek ini —
  tiap kali menemukan & memperbaiki bug di mesin pencocokan, tambah test yang
  mereplikasi skenario data nyata yang gagal (lihat `tests/test_engine.py`), bukan
  cuma memperbaiki kodenya.

## Menambah parser bank baru

Ikuti pola `apps/ingest/parsers/bri.py` / `bca.py` / `mandiri.py`:

1. Buat `apps/ingest/parsers/<bank>.py` dengan fungsi `parse(content: str | bytes) -> ParseResult`
   (lihat `ParsedBankRow`/`ParseResult` di `apps/ingest/parsers/base.py`).
2. Daftarkan di `apps/ingest/parsers/__init__.py` (dispatcher `parse_file`).
3. Tambah `Channel` baru di `apps/core/enums.py` kalau memang bank baru (bukan
   format baru dari bank yang sudah ada).
4. Tambah test di `tests/test_parsers.py` dengan sampel baris mentah asli (anonim).

## Menambah management command

Ikuti pola `apps/recon/management/commands/recon_match.py` — `BaseCommand` tipis
yang cuma memanggil fungsi service (`apps/recon/engine.py`, `apps/recon/resolve.py`,
dst). Command yang mengubah data (bukan cuma laporan) sebaiknya punya mode dry-run
default + flag `--apply` eksplisit — lihat `cleanup_qris_amount_diff.py` sebagai
contoh, karena ini aplikasi finansial yang sedang berjalan di production.

## Menambah halaman dashboard baru

1. Buat `apps/dashboard/views/<nama_halaman>.py` (satu file = satu grup halaman,
   ikuti pola yang sudah ada).
2. Re-export fungsi view-nya di `apps/dashboard/views/__init__.py`.
3. Daftarkan route di `apps/dashboard/urls.py`.
4. Kalau ada state lintas-halaman yang dibutuhkan (mis. helper pencarian pasangan
   otomatis), taruh di `apps/dashboard/views/_shared.py`, jangan duplikasi.

## Deploy

Lihat [README.md](README.md) bagian "Deploy ZimaOS" untuk langkah lengkap. Singkatnya:
`git pull` lalu `docker compose -f compose.zima.yml up -d --build` — command start
container sudah otomatis `migrate --noinput`, jadi migration baru langsung ter-apply.
**`git pull` saja tidak cukup** — image Docker baru pakai kode terbaru setelah
di-build ulang.
