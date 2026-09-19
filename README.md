# Laporan Server — Rekonsiliasi Harian

Mencocokkan uang masuk di rekening bank (BRI / BCA / Merchant BCA QRIS / Mandiri)
dengan penambahan saldo reseller di OTOMAX, per tanggal buku. Selisih yang belum
ketemu dibiarkan terbuka dan bisa diselesaikan dari hari berikutnya lewat
penyesuaian bertanggal — hari yang sudah ditutup tidak pernah diedit.

Desain: lihat artifact **Rekonsiliasi Harian BRI–OTOMAX** & **Cetak Biru Rekonsiliasi**.

## Struktur Kode

Penjelasan lengkap struktur apps, alur pipeline rekonsiliasi, dan aturan bisnis yang
tidak jelas dari nama fungsi (mis. kenapa netting REV pakai `ref_core`, apa itu
`PENDING_SETTLE`) ada di **[ARCHITECTURE.md](ARCHITECTURE.md)**. Konvensi menulis
kode & cara menambah fitur (parser bank baru, halaman dashboard baru, dll) ada di
**[CONTRIBUTING.md](CONTRIBUTING.md)**.

## Stack

Django 5 · Postgres 16 (pg_trgm) · HTMX + Alpine + Tailwind (CLI standalone, tanpa Node)
· Decimal / polars / rapidfuzz untuk perhitungan.

## Dev (MacBook Air)

```bash
# 1. dependensi
uv sync

# 2. database
docker compose -f compose.dev.yml up -d
cp .env.example .env            # DATABASE_URL sudah menunjuk ke port 5433

# 3. CSS (sekali, atau --watch di terminal terpisah)
curl -fsSL -o ./tailwindcss \
  https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.14/tailwindcss-macos-arm64
chmod +x ./tailwindcss
./tailwindcss -i apps/dashboard/static/src/input.css -o apps/dashboard/static/css/tailwind.css --watch

# 4. migrasi + admin + jalan
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver 8789
```

Tanpa Docker: hapus `DATABASE_URL` dari `.env` → otomatis pakai SQLite (`db.sqlite3`).

## Alur pemakaian

```bash
uv run python manage.py import_file BRI     ~/Downloads/bri.txt      2026-09-05
uv run python manage.py import_file OTOMAX  ~/Downloads/otomax.txt   2026-09-05
uv run python manage.py recon_match 2026-09-05        # cocokkan + carry-forward
uv run python manage.py recon_close 2026-09-05        # tutup buku (snapshot + kunci)
```

Atau lewat UI di `/` (unggah → jalankan pencocokan → tutup buku).

## Test

```bash
uv run pytest
```

## Deploy ZimaOS (Port 3300)

### Cara 1: Lewat Terminal / SSH ZimaOS
```bash
# 1. Salin project ke ZimaOS atau git clone
git clone <repo-url> /DATA/AppData/laporanServer
cd /DATA/AppData/laporanServer

# 2. Siapkan file .env
cp .env.example .env
# Edit .env sesuaikan IP ZimaOS Anda (misal: 192.168.1.50) di CSRF_TRUSTED_ORIGINS dan DB_PASSWORD

# 3. Jalankan container
docker compose -f compose.zima.yml up -d --build
```

### Cara 2: Lewat Web UI ZimaOS (Custom App)
1. Buka dashboard ZimaOS / CasaOS.
2. Klik tombol **+** di pojok kiri atas -> pilih **Install a customized app**.
3. Klik tombol **Import** (ikon terminal/dokumen di pojok kanan atas modal).
4. Salin seluruh isi file `compose.zima.yml` dan paste ke dalamnya.
5. Lengkapi Environment Variables (`SECRET_KEY`, `DB_PASSWORD`, `DATABASE_URL`, `CSRF_TRUSTED_ORIGINS`).
6. Port aplikasi otomatis diset ke `3300`. Klik **Install**.

Aplikasi dapat diakses di browser: `http://<IP_ZIMAOS>:3300`
- Web dashboard: port `3300`
- Database: Postgres 16 di `./data/pg`
- Otomatis backup: Setiap hari ke `./data/backups` (retensi 14 hari)
- Login awal: Username `admin` / Password `admin12345` (sesuai `.env`)
# LServer
