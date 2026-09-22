"""Halaman 2: Upload Data & Preview."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.enums import Channel
from apps.ingest.models import ImportBatch
from apps.ingest.services import ImportBlocked, import_file, preview_file
from apps.recon.purge import DayIsClosed, delete_import_batch

from ._shared import _parse_date, _sync_inconsistent_batches, build_today_steps


@login_required
def upload_view(request):
    _sync_inconsistent_batches()
    book_date = _parse_date(request.GET.get("d") or request.POST.get("book_date"))
    batches = ImportBatch.objects.filter(book_date=book_date).order_by("-created_at")

    preview_data = None
    if request.method == "POST":
        action = request.POST.get("action")
        channel = request.POST.get("channel")
        upload_file = request.FILES.get("file")

        if not upload_file or channel not in Channel.values:
            messages.error(request, "Pilih channel dan file yang valid.")
            return redirect(f"/upload/?d={book_date}")

        content = upload_file.read()
        filename = upload_file.name

        if action == "preview":
            try:
                preview_data = preview_file(channel, content, book_date=book_date)
                preview_data["filename"] = filename
            except Exception as exc:
                messages.error(request, f"Gagal membaca preview: {exc}")
        else:
            # Action == 'import'
            try:
                batch = import_file(
                    channel=channel,
                    content=content,
                    book_date=book_date,
                    filename=filename,
                    user=request.user,
                )
                if batch.book_date != book_date:
                    messages.success(
                        request,
                        f"Berhasil mengimpor {batch.channel}: {batch.row_count} baris. "
                        f"Tanggal transaksi terdeteksi {batch.book_date.strftime('%d %b %Y')}, "
                        f"data otomatis disimpan dan dialihkan ke tanggal tersebut.",
                    )
                else:
                    messages.success(
                        request,
                        f"Berhasil mengimpor {batch.channel}: {batch.row_count} baris "
                        f"({batch.quarantined_count} dikarantina).",
                    )
                return redirect(f"/upload/?d={batch.book_date}")
            except ImportBlocked as exc:
                messages.error(request, str(exc))
            except Exception as exc:
                messages.error(request, f"Gagal mengimpor file: {exc}")

    return render(
        request,
        "dashboard/upload.html",
        {
            "book_date": book_date,
            "channels": Channel.choices,
            "batches": batches,
            "preview": preview_data,
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
        if batch.book_date != book_date:
            messages.success(
                request,
                f"{batch.channel}: {batch.row_count} baris. "
                f"Terdeteksi tanggal transaksi {batch.book_date.strftime('%d %b %Y')}, "
                f"data otomatis dialihkan ke tanggal tersebut.",
            )
        else:
            messages.success(
                request,
                f"{batch.channel}: {batch.row_count} baris ({batch.quarantined_count} dikarantina).",
            )
        return redirect(f"/upload/?d={batch.book_date}")
    except ImportBlocked as exc:
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
