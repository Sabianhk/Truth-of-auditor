from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render

from reconciliation.engine import MATCHERS, check_completeness, run_batch, run_match
from reconciliation.models import MatchResult, MatchRun, ReconBatch, ReviewAction, ToleranceProfile
from sources.detect import detect_source
from sources.management.commands.ingest import detect_flow
from sources.models import SourceType, Upload
from sources.services import PARSERS, ingest
from transactions.models import Transaction
from web.access import tokos_for

BUCKET_META = {
    "cocok": {"label": "Cocok", "cls": "ok"},
    "perlu_tinjau": {"label": "Perlu Ditinjau", "cls": "warn"},
    "tidak_cocok": {"label": "Tidak Cocok", "cls": "bad"},
}


def _active_toko(request):
    allowed = tokos_for(request.user)
    tid = request.session.get("active_toko_id")
    t = allowed.filter(id=tid).first() if tid else None
    return t or allowed.first()


@login_required
def set_toko(request):
    if request.method == "POST":
        tid = request.POST.get("toko_id", "")
        if tid.isdecimal() and tokos_for(request.user).filter(id=tid).exists():
            request.session["active_toko_id"] = int(tid)
    return redirect(request.POST.get("next") or "dashboard")


@login_required
def dashboard(request):
    active = _active_toko(request)
    if active is None:
        return render(request, "web/no_toko.html")
    tx = Transaction.objects.filter(toko=active)
    uploads = Upload.objects.filter(toko=active)
    runs = MatchRun.objects.filter(batch__toko=active)
    by_source = list(
        tx.values("source_type__name", "source_type__key")
        .annotate(n=Count("id"))
        .order_by("-n")
    )
    ctx = {
        "active_toko": active,
        "tx_total": tx.count(),
        "upload_total": uploads.count(),
        "run_total": runs.count(),
        "by_source": by_source,
        "uploads": uploads.select_related("source_type").order_by("-id")[:8],
        "runs": runs.order_by("-id")[:8],
        "batches": ReconBatch.objects.filter(toko=active).order_by("-id")[:10],
    }
    return render(request, "web/dashboard.html", ctx)


@login_required
def upload(request):
    active = _active_toko(request)
    if active is None:
        return render(request, "web/no_toko.html")
    if request.method == "POST" and request.POST.get("action") == "commit":
        staged = request.POST.getlist("staged")
        keys = request.POST.getlist("parser_key")
        flows = request.POST.getlist("flow")
        provider = request.POST.get("provider", "")
        n_ok = n_err = 0
        for path_rel, key, flow in zip(staged, keys, flows):
            if key not in PARSERS:
                n_err += 1
                continue
            try:
                ingest(
                    key, default_storage.path(path_rel), flow=flow,
                    user=request.user, toko=active, provider=provider,
                )
                n_ok += 1
            except Exception as e:  # noqa: BLE001 - tampilkan error parse ke user
                messages.error(request, f"{path_rel}: {e}")
                n_err += 1
            finally:
                if default_storage.exists(path_rel):
                    default_storage.delete(path_rel)
        messages.success(request, f"{n_ok} file diproses, {n_err} gagal.")
        return redirect("upload")
    if request.method == "POST" and request.POST.get("action") == "analyze":
        preview = []
        for f in request.FILES.getlist("files"):
            saved = default_storage.save(f"staging/{f.name}", f)
            cands = detect_source(default_storage.path(saved), f.name)
            top = cands[0] if cands else None
            preview.append({
                "name": f.name,
                "staged": saved,
                "parser_key": top["parser_key"] if top else "",
                "confidence": round(top["confidence"] * 100) if top else 0,
                "needs_confirm": (top is None) or top["confidence"] < 0.8,
                "flow": detect_flow(f.name),
            })
        return render(request, "web/upload.html", {
            "preview": preview, "parsers": sorted(PARSERS.keys()),
            "flows": ["", "dp", "wd"], "active_toko": active,
            "uploads": Upload.objects.filter(toko=active).select_related("source_type").order_by("-id")[:20],
        })
    return render(request, "web/upload.html", {
        "parsers": sorted(PARSERS.keys()), "active_toko": active,
        "uploads": Upload.objects.filter(toko=active).select_related("source_type").order_by("-id")[:20],
    })


@login_required
def transactions(request):
    active = _active_toko(request)
    if active is None:
        return render(request, "web/no_toko.html")
    qs = Transaction.objects.filter(toko=active).select_related("source_type").order_by("-occurred_at")
    src = request.GET.get("source", "")
    jenis = request.GET.get("jenis", "")
    q = request.GET.get("q", "").strip()
    if src:
        qs = qs.filter(source_type__key=src)
    if jenis:
        qs = qs.filter(jenis=jenis)
    if q:
        qs = qs.filter(
            Q(username__icontains=q)
            | Q(ticket_no__icontains=q)
            | Q(reference__icontains=q)
            | Q(counterparty__icontains=q)
        )
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    ctx = {
        "page": page,
        "sources": SourceType.objects.all(),
        "jenis_choices": Transaction.Jenis.choices,
        "src": src,
        "jenis": jenis,
        "q": q,
        "total": qs.count(),
    }
    return render(request, "web/transactions.html", ctx)


@login_required
def reconcile(request):
    active = _active_toko(request)
    if active is None:
        return render(request, "web/no_toko.html")
    if request.method == "POST":
        tol = ToleranceProfile.objects.get(name=request.POST.get("tolerance", "Default"))
        batch = run_batch(
            active, tol,
            request.POST.get("date_from") or None,
            request.POST.get("date_to") or None,
            user=request.user,
        )
        messages.success(request, f"Rekonsiliasi selesai (Batch #{batch.pk}).")
        return redirect("batch_detail", pk=batch.pk)

    df = request.GET.get("date_from") or None
    dt = request.GET.get("date_to") or None
    ctx = {
        "active_toko": active,
        "completeness": check_completeness(active, df, dt),
        "tolerances": ToleranceProfile.objects.all(),
        "batches": ReconBatch.objects.filter(toko=active).order_by("-id")[:20],
        "date_from": df or "", "date_to": dt or "",
    }
    return render(request, "web/reconcile.html", ctx)


@login_required
def batch_detail(request, pk):
    batch = get_object_or_404(ReconBatch, pk=pk, toko__in=tokos_for(request.user))
    return render(request, "web/batch_detail.html", {
        "batch": batch, "s": batch.summary or {}, "runs": batch.runs.all(),
    })


@login_required
def run_detail(request, pk):
    run = get_object_or_404(MatchRun, pk=pk, batch__toko__in=tokos_for(request.user))
    qs = MatchResult.objects.filter(run=run).select_related("left", "right").order_by("bucket", "-score")
    bucket = request.GET.get("bucket", "")
    if bucket:
        qs = qs.filter(bucket=bucket)
    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    ctx = {"run": run, "page": page, "bucket": bucket, "bucket_meta": BUCKET_META}
    return render(request, "web/run_detail.html", ctx)


@login_required
def review(request, pk):
    r = get_object_or_404(MatchResult, pk=pk, run__batch__toko__in=tokos_for(request.user))
    action = request.POST.get("action", "")
    reason = request.POST.get("reason", "")
    if action == "mark_matched":
        r.bucket = MatchResult.Bucket.COCOK
    elif action == "mark_review":
        r.bucket = MatchResult.Bucket.TINJAU
    elif action == "mark_unmatched":
        r.bucket = MatchResult.Bucket.TIDAK
    r.reason_code = "manual_override"
    r.save(update_fields=["bucket", "reason_code"])
    ReviewAction.objects.create(result=r, action=action, reason=reason, reviewer=request.user)
    return render(request, "web/_result_row.html", {"r": r, "bucket_meta": BUCKET_META})


@login_required
def export_run(request, pk):
    import io

    from django.http import HttpResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font

    run = get_object_or_404(MatchRun, pk=pk, batch__toko__in=tokos_for(request.user))
    wb = Workbook()
    ws = wb.active
    ws.title = "Ringkasan"
    s = run.summary or {}
    for label, val in [
        ("Rekonsiliasi", run.get_relation_display()),
        ("Run", f"#{run.pk}"),
        ("Toleransi", f"{run.tolerance.name} (±{run.tolerance.date_window_days} hari)"),
        ("Tanggal", run.created_at.strftime("%d/%m/%Y %H:%M")),
        ("", ""),
        ("Cocok", s.get("cocok", 0)),
        ("Perlu Ditinjau", s.get("perlu_tinjau", 0)),
        ("Tidak Cocok", s.get("tidak_cocok", 0)),
    ]:
        ws.append([label, val])
    for row in ws["A"]:
        row.font = Font(bold=True)

    d = wb.create_sheet("Hasil")
    headers = ["Bucket", "Panel Ticket", "Panel Amount", "Panel User", "Panel Waktu",
               "Kanan", "Kanan Sumber", "Kanan Amount", "Kanan Waktu", "Skor", "Alasan", "Detail"]
    d.append(headers)
    for c in d[1]:
        c.font = Font(bold=True)
    qs = run.results.select_related("left", "right", "left__source_type", "right__source_type")
    for r in qs.iterator():
        left, right = r.left, r.right
        d.append([
            r.get_bucket_display(),
            left.ticket_no if left else "",
            float(left.amount) if left else "",
            left.username if left else "",
            left.occurred_at.strftime("%d/%m %H:%M") if left and left.occurred_at else "",
            (right.ticket_no or right.counterparty) if right else "",
            right.source_type.key if right else "",
            float(right.amount) if right else "",
            right.occurred_at.strftime("%d/%m %H:%M") if right and right.occurred_at else "",
            round(r.score or 0),
            r.reason_code,
            r.reason_detail,
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = HttpResponse(
        buf.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f'attachment; filename="rekonsiliasi_run{run.pk}.xlsx"'
    return resp
