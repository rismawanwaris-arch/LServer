# Laporan Server — Rekonsiliasi Harian

Mencocokkan uang masuk di rekening bank (BRI / BCA / Merchant BCA QRIS / Mandiri)
dengan penambahan saldo reseller di OTOMAX, per tanggal buku. Selisih yang belum
ketemu dibiarkan terbuka dan bisa diselesaikan dari hari berikutnya lewat
penyesuaian bertanggal — hari yang sudah ditutup tidak pernah diedit.

Desain: lihat artifact **Rekonsiliasi Harian BRI–OTOMAX** & **Cetak Biru Rekonsiliasi**.

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

## Deploy ZimaOS

```bash
git clone ... && cd LaporanServer
cp .env.example .env      # isi SECRET_KEY, DB_PASSWORD, ALLOWED_HOSTS
docker compose -f compose.zima.yml up -d --build
```

App di `:8091`. Postgres bind-mount ke `./data/pg`, backup harian ke `./data/backups`.
