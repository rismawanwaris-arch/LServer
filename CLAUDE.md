# Laporan Server — Claude Code Project Guidelines

Rekonsiliasi harian mutasi bank (BRI / BCA / Merchant BCA QRIS / Mandiri) vs OTOMAX.

## Quick Facts
- **Stack**: Django 5.1, PostgreSQL 16 (dev port 5433 / prod port 3300), SQLite local fallback
- **Frontend**: Django Templates + HTMX + Alpine.js + Tailwind CSS (CLI standalone, tanpa Node)
- **Package Manager**: `uv`
- **Test Command**: `uv run pytest`
- **Lint Command**: `uv run ruff check apps/`
- **Format Command**: `uv run ruff format apps/`
- **Django Check**: `uv run python manage.py check`

## Key Directories
- `apps/core/` — Model dasar (`TimeStampedModel`, `money_field`), enum bersama (`apps/core/enums.py`), fungsi murni normalisasi teks (`apps/core/normalize.py`).
- `apps/catalog/` — Data referensi (`Reseller`, `ResellerAlias`, `MerchantMap`, `ExclusionRule`).
- `apps/ingest/` — Parsers file mentah (`apps/ingest/parsers/*.py`) dan ingest service (`services.py`).
- `apps/recon/` — Engine pencocokan (`engine/`), carry-forward (`carry.py`), tutup buku (`close.py`), resolusi manual (`resolve.py`).
- `apps/dashboard/` — UI dashboard (views dipecah per file di `apps/dashboard/views/`).
- `apps/api/` — Endpoint REST tipis.
- `tests/` — Test suite pytest (`test_engine.py`, `test_parsers.py`, dll).

## Aturan Bisnis & Arsitektur Kritis
1. **Uang Wajib `Decimal`**: Jangan pernah gunakan `float` untuk angka uang. Gunakan helper `money_field()` (`DecimalField(max_digits=15, decimal_places=2)`).
2. **Kandidat Pencocokan (`_OPEN_STATUSES`)**: Status terbuka ada dua: `MatchStatus.UNMATCHED` dan `MatchStatus.PENDING_SETTLE`. Query pencocokan baru WAJIB memakai `match_status__in=_OPEN_STATUSES` (`apps.recon.engine.helpers._OPEN_STATUSES`). Jangan pernah filter hanya `match_status=MatchStatus.UNMATCHED`.
3. **Netting REVERSAL Pakai `ref_core`**: Netting REVERSAL menggunakan token angka inti (`ref_core`), bukan teks persis (`ref_normalized`), karena Otomax sering menambahkan format tanggal hanya pada baris revisi.
4. **Day-Lock & Append-Only**: Tanggal buku yang sudah ditutup (`ReconDay.locked=True`) tidak boleh diedit atau dihapus. Penyelesaian selisih susulan dilakukan via `Adjustment` bertanggal hari asal.
5. **Regression Test untuk Bugfix**: Setiap kali memperbaiki bug di parser atau engine rekonsiliasi, WAJIB menambahkan test baru di `tests/` yang mereplikasi data mentah gagal sebelum menulis fix.
6. **Autentikasi Dashboard**: Semua view di `apps/dashboard/views/` harus menggunakan `@login_required`.

## Skills Tersedia
- `.claude/skills/htmx-patterns/SKILL.md`: Pola parsial HTMX, request detection, loading indicator, dan UI feedback.
- `.claude/skills/pytest-django-patterns/SKILL.md`: Pola pengujian pytest-django, TDD, database isolation, dan fixtures.
- `.claude/skills/django-models/SKILL.md`: Desain model ORM, optimasi QuerySet (N+1 prevention), dan data integrity.
- `.claude/skills/systematic-debugging/SKILL.md`: Metodologi debugging 4-fase (Reproduce -> Isolate -> Root Cause -> Fix & Verify).
- `.claude/skills/ui-ux-pro-max/SKILL.md`: Panduan desain UI/UX + pencarian lokal gaya/warna/tipografi (`python3 .claude/skills/ui-ux-pro-max/scripts/search.py "<query>" --stack html-tailwind`). Diambil dari nextlevelbuilder/ui-ux-pro-max-skill @09170ee (MIT), sudah dipindai: stdlib saja, tanpa jaringan/subprocess.
