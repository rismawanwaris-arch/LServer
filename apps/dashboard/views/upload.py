"""Halaman 2: Upload Data & Preview."""

from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.enums import Channel
from apps.ingest.models import ImportBatch
from apps.ingest.review import analyze_upload
from apps.ingest.services import ImportBlocked, NothingToImport, import_file
from apps.ingest.staging import (
    delete_staged_upload,
    get_staged_upload,
    stage_upload,
)
from apps.recon.purge import DayIsClosed, delete_import_batch

from ._shared import _parse_date, _sync_inconsistent_batches, build_today_steps


def _format_import_success_message(batch: ImportBatch, original_book_date: date) -> str:
    rev = getattr(batch, "review", {})
    saved = rev.get("saved", batch.row_count)
    skipped_details = []
    if rev.get("existing"):
        skipped_details.append(f"{rev['existing']} sudah ada")
    if rev.get("maybe_skipped"):
        skipped_details.append(f"{rev['maybe_skipped']} kemungkinan sudah ada")
    if rev.get("dup_file"):
        skipped_details.append(f"{rev['dup_file']} kembar di file")
    if rev.get("locked"):
        skipped_details.append(f"{rev['locked']} di hari tutup buku")

    if skipped_details:
        msg = f"Berhasil mengimpor {batch.channel}: {saved} baris ditambahkan, {', '.join(skipped_details)} dilewati."
    else:
        msg = f"Berhasil mengimpor {batch.channel}: {saved} baris ditambahkan ({batch.quarantined_count} dikarantina)."

    if batch.book_date != original_book_date:
        msg += (
            f" (Tanggal transaksi terdeteksi {batch.book_date.strftime('%d %b %Y')}, "
            f"data otomatis disimpan dan dialihkan ke tanggal tersebut)"
        )
    return msg


@login_required
def upload_view(request):
    _sync_inconsistent_batches()
    book_date = _parse_date(request.GET.get("d") or request.POST.get("book_date"))
    batches = ImportBatch.objects.filter(book_date=book_date).order_by("-created_at")

    review_data = None
    staging_token = None
    selected_channel = request.POST.get("channel") or "OTOMAX"

    if request.method == "POST":
        action = request.POST.get("action", "review")

        if action == "cancel":
            token = request.POST.get("token", "")
            staged = get_staged_upload(token)
            # Hanya pengunggahnya yang boleh membatalkan / menyimpan file yang ditahan.
            if staged and staged.user_id in (None, request.user.id):
                delete_staged_upload(token)
            messages.info(request, "Pemeriksaan file dibatalkan.")
            return redirect(f"/upload/?d={book_date}")

        elif action == "confirm":
            token = request.POST.get("token", "")
            staged = get_staged_upload(token)
            if staged and staged.user_id not in (None, request.user.id):
                staged = None  # token milik pengguna lain: perlakukan seperti tidak ada
            if not staged:
                messages.error(
                    request,
                    "Sesi periksa file telah kedaluwarsa atau tidak valid. Silakan pilih dan periksa file kembali.",
                )
                return redirect(f"/upload/?d={book_date}")

            include_maybe = set(request.POST.getlist("include_maybe"))
            try:
                batch = import_file(
                    channel=staged.channel,
                    content=staged.content,
                    book_date=staged.book_date,
                    filename=staged.filename,
                    user=request.user,
                    include_maybe=include_maybe,
                )
                delete_staged_upload(token)
                messages.success(request, _format_import_success_message(batch, book_date))
                return redirect(f"/upload/?d={batch.book_date}")
            except (ImportBlocked, NothingToImport) as exc:
                delete_staged_upload(token)
                messages.error(request, str(exc))
                return redirect(f"/upload/?d={staged.book_date}")
            except Exception as exc:
                delete_staged_upload(token)
                messages.error(request, f"Gagal mengimpor file: {exc}")
                return redirect(f"/upload/?d={staged.book_date}")

        elif action in ("review", "preview"):
            channel = request.POST.get("channel")
            upload_file = request.FILES.get("file")

            if not upload_file or channel not in Channel.values:
                messages.error(request, "Pilih channel dan file yang valid.")
                return redirect(f"/upload/?d={book_date}")

            selected_channel = channel
            content = upload_file.read()
            filename = upload_file.name

            try:
                staging_token = stage_upload(
                    channel=channel,
                    book_date=book_date,
                    filename=filename,
                    content=content,
                    user_id=request.user.id,
                )
                review_data = analyze_upload(channel, content, book_date, filename=filename)
            except Exception as exc:
                messages.error(request, f"Gagal memeriksa file: {exc}")
                return redirect(f"/upload/?d={book_date}")

        elif action == "import":
            # Direct import (fallback / bypass)
            channel = request.POST.get("channel")
            upload_file = request.FILES.get("file")

            if not upload_file or channel not in Channel.values:
                messages.error(request, "Pilih channel dan file yang valid.")
                return redirect(f"/upload/?d={book_date}")

            content = upload_file.read()
            filename = upload_file.name

            try:
                batch = import_file(
                    channel=channel,
                    content=content,
                    book_date=book_date,
                    filename=filename,
                    user=request.user,
                )
                messages.success(request, _format_import_success_message(batch, book_date))
                return redirect(f"/upload/?d={batch.book_date}")
            except (ImportBlocked, NothingToImport) as exc:
                messages.error(request, str(exc))
            except Exception as exc:
                messages.error(request, f"Gagal mengimpor file: {exc}")
            return redirect(f"/upload/?d={book_date}")

    return render(
        request,
        "dashboard/upload.html",
        {
            "book_date": book_date,
            "channels": Channel.choices,
            "batches": batches,
            "review": review_data,
            "staging_token": staging_token,
            "selected_channel": selected_channel,
            "today_steps": build_today_steps(book_date),
        },
    )


@login_required
@require_POST
def upload(request):
    """Legacy redirect handler for upload."""
    book_date = _parse_date(request.POST.get("book_date"))
    channel = request.POST.get("channel")
    upload_file = request.FILES.get("file")
    if not upload_file or channel not in Channel.values:
        messages.error(request, "Pilih channel dan file.")
        return redirect(f"/upload/?d={book_date}")
    try:
        batch = import_file(
            channel=channel,
            content=upload_file.read(),
            book_date=book_date,
            filename=upload_file.name,
            user=request.user,
        )
        messages.success(request, _format_import_success_message(batch, book_date))
        return redirect(f"/upload/?d={batch.book_date}")
    except (ImportBlocked, NothingToImport) as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, f"Gagal: {exc}")
    return redirect(f"/upload/?d={book_date}")


@login_required
@require_POST
def delete_batch_action(request, pk: int):
    book_date_raw = request.POST.get("book_date")
    try:
        res = delete_import_batch(pk)
        bdate = res["book_date"]
        unlinked_msg = (
            f" ({res['matches_unlinked']} pasangan terkait direset ke belum cocok)."
            if res["matches_unlinked"] > 0
            else ""
        )
        messages.success(
            request,
            f"Batch {res['channel']} ({res['filename']}) berhasil dihapus. "
            f"{res['row_count']} baris data telah dibersihkan{unlinked_msg}",
        )
        return redirect(f"/upload/?d={bdate}")
    except DayIsClosed as exc:
        messages.error(request, str(exc))
    except Exception as exc:
        messages.error(request, f"Gagal menghapus batch: {exc}")

    target_d = book_date_raw or ""
    return redirect(f"/upload/?d={target_d}" if target_d else "/upload/")
