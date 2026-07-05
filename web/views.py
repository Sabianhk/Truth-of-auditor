import os
import zipfile
from collections import Counter
from datetime import date, timedelta

from django.contrib import messages
from django.core.files.base import ContentFile
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.core.paginator import Paginator
from django.db.models import BooleanField, Count, Exists, ExpressionWrapper, Max, Min, OuterRef, Q, Sum
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from reconciliation.engine import (
    MATCHERS,
    MONEY_SOURCES,
    check_completeness,
    rematch_batch,
    run_batch,
    run_batch_into,
    run_match,
)
from core.audit import catat
from core.models import AuditLog
from reconciliation.models import MatchResult, MatchRun, ReconBatch, ReviewAction, ToleranceProfile
from sources.detect import detect_source
from sources.management.commands.ingest import detect_flow
from sources.models import SourceType, Upload
from sources.services import PARSERS, ingest, is_encrypted_xlsx
from transactions.models import Transaction, specific_source_label
from web.access import tokos_for

BUCKET_META = {
    "cocok": {"label": "Cocok", "cls": "ok"},
    "perlu_tinjau": {"label": "Perlu Ditinjau", "cls": "warn"},
    "tidak_cocok": {"label": "Tidak Cocok", "cls": "bad"},
}

REL_LABELS = {
    MatchRun.Relation.PANEL_BRACKET.value: ("Panel", "Bracket"),
    MatchRun.Relation.PANEL_BANK.value: ("Panel", "Bank/Gateway"),
    MatchRun.Relation.BRACKET_BANK.value: ("Bracket", "Bank/Gateway"),
    MatchRun.Relation.SALDO.value: ("Kiri", "Kanan"),
}


def csrf_failure(request, reason=""):
    """Token CSRF basi (tab lama / setelah redeploy) — jangan 403 mentah.

    Logout: risiko CSRF-nya sepele (paling banter dipaksa keluar), jadi
    selesaikan saja logout-nya. Selain itu: halaman ramah + link masuk.
    """
    if request.path == reverse("logout"):
        auth_logout(request)
        return redirect("login")
    return render(request, "web/csrf_failure.html", status=403)


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
    nxt = request.POST.get("next")
    if nxt and url_has_allowed_host_and_scheme(
        nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(nxt)
    return redirect("dashboard")


def _dashboard_health(toko):
    """KPI kesehatan audit: agregat ringan dari summary 30 batch terakhir.

    Menjawab tiga pertanyaan ritual harian saat mendarat: (a) ada selisih
    terbuka? (b) berapa hari belum direkonsiliasi? (c) berapa batch masih
    punya ekor tidak_cocok yang menunggu uang susulan."""
    recent = list(ReconBatch.objects.filter(toko=toko).order_by("-id")[:30])
    selisih_total, ekor = 0, 0
    buckets = {"cocok": 0, "perlu_tinjau": 0, "tidak_cocok": 0}
    for b in recent:
        s = b.summary or {}
        selisih_total += abs((s.get("dp") or {}).get("selisih") or 0)
        selisih_total += abs((s.get("wd") or {}).get("selisih") or 0)
        bk = s.get("buckets") or {}
        if (bk.get("tidak_cocok") or 0) > 0:
            ekor += 1
        for k in buckets:
            buckets[k] += bk.get(k) or 0
    today = date.today()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    per_day = dict.fromkeys(days, 0)
    for b in recent:
        if b.date_to in per_day:
            s = b.summary or {}
            per_day[b.date_to] += abs((s.get("dp") or {}).get("selisih") or 0)
            per_day[b.date_to] += abs((s.get("wd") or {}).get("selisih") or 0)
    saran = _saran_tanggal(toko)
    return {
        "selisih_terbuka": selisih_total,
        "ekor_terbuka": ekor,
        "buckets_agg": buckets,
        "selisih_trend": [per_day[d] for d in days],
        "saran_tanggal": saran,
        "hari_tertunda": max((today - saran).days + 1, 0) if saran else 0,
    }


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
        "health": _dashboard_health(active),
    }
    return render(request, "web/dashboard.html", ctx)


def _rematch_candidates(toko, money_uploads):
    """Setelah upload sumber UANG: batch lama yang berpotensi ditutup mutasi baru
    (ekor malam T+1 / statement BNI bulanan). Kandidat: toko sama, punya baris
    tidak_cocok, window overlap rentang tanggal transaksi baru
    (batch.date_from <= max & batch.date_to >= min-1hari).

    Urut TERTUA dulu — auto re-match memberi hari lebih awal pilihan pertama atas
    uang yang sama. Kembalikan list (batch, nomor_per_toko), maks 10.
    """
    if not money_uploads:
        return []
    agg = Transaction.objects.filter(upload__in=money_uploads).aggregate(
        lo=Min("occurred_at"), hi=Max("occurred_at")
    )
    lo, hi = agg["lo"], agg["hi"]
    if lo is None or hi is None:
        return []
    hi_d = hi.date()
    lo_slack = lo.date() - timedelta(days=1)  # aproksimasi window toleransi 1 hari

    recent = ReconBatch.objects.filter(toko=toko).order_by("-id")[:30]
    hits = []
    for b in sorted(recent, key=lambda b: b.id):
        if (b.summary or {}).get("buckets", {}).get("tidak_cocok", 0) <= 0:
            continue
        # Tanpa batas tanggal (None) = seluruh rentang → selalu overlap.
        if b.date_from is not None and b.date_from > hi_d:
            continue
        if b.date_to is not None and b.date_to < lo_slack:
            continue
        no = ReconBatch.objects.filter(toko=toko, id__lte=b.id).count()
        hits.append((b, no))
        if len(hits) == 10:
            break
    return hits


def _selisih_abs(batch):
    """Total |selisih| DP+WD dari summary batch — angka delta kartu penyembuhan."""
    s = batch.summary or {}
    return abs((s.get("dp") or {}).get("selisih") or 0) + abs((s.get("wd") or {}).get("selisih") or 0)


def _auto_rematch(toko, money_uploads, user=None):
    """Re-match otomatis batch kandidat setelah upload sumber uang — tanpa klik.

    Kembalikan list dict terstruktur per batch (delta selisih before→after)
    untuk kartu laporan penyembuhan. Batch tanpa baris terpasang tidak
    dilaporkan (anti-noise); error per-batch dilaporkan tapi tidak
    menggagalkan upload (file sudah ter-ingest)."""
    out = []
    for batch, no in _rematch_candidates(toko, money_uploads):
        before = _selisih_abs(batch)
        try:
            stats = rematch_batch(batch, user=user)
        except Exception as e:  # noqa: BLE001 - upload jangan ikut gagal
            out.append({"level": "error", "batch_pk": batch.pk, "batch_no": no, "error": str(e)})
            continue
        if stats["terpasang"]:
            batch.refresh_from_db()
            out.append({
                "level": "success", "batch_pk": batch.pk, "batch_no": no,
                "terpasang": stats["terpasang"], "cocok": stats["cocok"],
                "perlu_tinjau": stats["perlu_tinjau"],
                "selisih_before": before, "selisih_after": _selisih_abs(batch),
            })
    return out


# Upload folder/zip: hanya ekstensi yang punya parser; sisanya junk OS/temp.
_UPLOAD_EXTS = {".xlsx", ".xls", ".csv", ".pdf"}
_ZIP_MAX_FILES = 200
_ZIP_MAX_BYTES = 200 * 1024 * 1024
# Cap ukuran: per-file dan total satu request analyze — volume staging jangan
# bisa dipenuhi satu upload liar (export mutasi riil terbesar masih < 20MB).
_FILE_MAX_BYTES = 50 * 1024 * 1024
_REQ_MAX_BYTES = 300 * 1024 * 1024


def _is_junk_name(name):
    """File yang tak layak dianalisis: dotfile/.DS_Store, lock Office (~$),
    artefak __MACOSX, atau ekstensi tanpa parser. Berlaku untuk upload langsung,
    isi folder (webkitdirectory), dan isi zip."""
    path = str(name).replace("\\", "/")
    if "__MACOSX" in path:
        return True
    base = os.path.basename(path)
    if not base or base.startswith(".") or base.startswith("~$"):
        return True
    return os.path.splitext(base)[1].lower() not in _UPLOAD_EXTS


def _extract_zip(f):
    """Ekstrak arsip zip upload → (list[(nama, bytes)], n_dilewati, error|None).
    Guard: jumlah file & total ukuran terkompresi-buka (anti zip-bomb); zip
    berpassword/rusak → error berpesan jelas. xlsx TIDAK lewat sini (dicek
    berdasarkan ekstensi .zip, bukan magic PK — xlsx juga arsip zip)."""
    try:
        zf = zipfile.ZipFile(f)
    except zipfile.BadZipFile:
        return [], 0, "bukan file zip yang valid"
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > _ZIP_MAX_FILES:
        return [], 0, f"terlalu banyak file di dalam zip (>{_ZIP_MAX_FILES})"
    if sum(i.file_size for i in infos) > _ZIP_MAX_BYTES:
        return [], 0, "isi zip terlalu besar (>200MB)"
    out, dilewati = [], 0
    for i in infos:
        if _is_junk_name(i.filename):
            dilewati += 1
            continue
        try:
            data = zf.read(i)
        except RuntimeError:
            return [], 0, "zip berpassword tidak didukung — ekstrak dulu lalu upload isinya"
        out.append((os.path.basename(i.filename.replace("\\", "/")), data))
    return out, dilewati, None


# File staging lebih tua dari ini = yatim (analyze tanpa commit) → disapu.
_STAGING_TTL = 24 * 3600


def _sweep_staging():
    """Bersihkan file staging yatim. Dipanggil tiap analyze — murah (satu
    listdir), dan berjalan tepat saat volume dipakai lagi."""
    import time

    try:
        _dirs, files = default_storage.listdir("staging")
    except FileNotFoundError:
        return
    batas = time.time() - _STAGING_TTL
    for nama in files:
        rel = f"staging/{nama}"
        try:
            if os.path.getmtime(default_storage.path(rel)) < batas:
                default_storage.delete(rel)
        except OSError:
            continue


def _analyze_file(name, fileobj):
    """Satu file → baris preview (simpan ke staging + deteksi parser).
    Dipakai upload langsung maupun hasil ekstrak zip."""
    saved = default_storage.save(f"staging/{name}", fileobj)
    needs_password = is_encrypted_xlsx(default_storage.path(saved))
    cands = detect_source(default_storage.path(saved), name)
    top = cands[0] if cands else None
    parser_key = top["parser_key"] if top else ""
    if not parser_key and needs_password:
        parser_key = "mandiri"
    return {
        "name": name,
        "staged": saved,
        "parser_key": parser_key,
        "confidence": round(top["confidence"] * 100) if top else 0,
        "needs_confirm": (top is None) or top["confidence"] < 0.8,
        "needs_password": needs_password,
        "flow": detect_flow(name),
    }


def _uploads_for(toko, limit=20):
    """Riwayat upload toko, dianotasi `locked` (buktinya dipakai hasil rekon:
    direferensi MatchResult left/right ATAU dikonsumsi batch). Tombol Hapus
    per-baris dinonaktifkan; server (`_locking_batches`) tetap penjaga terakhir."""
    ref = MatchResult.objects.filter(
        Q(left__upload=OuterRef("pk")) | Q(right__upload=OuterRef("pk"))
    )
    consumed = Transaction.objects.filter(
        upload=OuterRef("pk"), consumed_by_batch__isnull=False
    )
    return (
        Upload.objects.filter(toko=toko)
        .select_related("source_type")
        .annotate(locked=ExpressionWrapper(
            Exists(ref) | Exists(consumed), output_field=BooleanField(),
        ))
        .order_by("-id")[:limit]
    )


def _saran_tanggal(toko):
    """Saran tanggal reconcile berikutnya: hari setelah window batch terakhir; bila
    belum ada batch berjendela, tanggal PANEL aktif tertua — ritual itu
    panel-driven; statement bank diekspor sebulan penuh (mulai tgl 1) dan tidak
    boleh menyeret saran ke awal bulan padahal data panel mulai belakangan.
    Fallback terakhir: transaksi aktif tertua apa pun. None = tanpa saran."""
    last = (
        ReconBatch.objects.filter(toko=toko, date_to__isnull=False)
        .order_by("-date_to", "-id")
        .first()
    )
    if last:
        return last.date_to + timedelta(days=1)
    aktif = Transaction.objects.filter(
        toko=toko, is_duplicate=False, consumed_by_batch__isnull=True
    )
    lo = aktif.filter(source_type__key="panel").aggregate(lo=Min("occurred_at"))["lo"]
    if lo is None:
        lo = aktif.aggregate(lo=Min("occurred_at"))["lo"]
    return lo.date() if lo else None


@login_required
def upload(request):
    active = _active_toko(request)
    if active is None:
        return render(request, "web/no_toko.html")
    if request.method == "POST" and request.POST.get("action") == "commit":
        staged = request.POST.getlist("staged")
        keys = request.POST.getlist("parser_key")
        flows = request.POST.getlist("flow")
        passwords = request.POST.getlist("password")
        provider = request.POST.get("provider", "")
        n_ok = n_err = 0
        money_uploads = []  # upload sumber uang → cek batch lama yg bisa di-re-match
        for i, (path_rel, key, flow) in enumerate(zip(staged, keys, flows)):
            if not path_rel.startswith("staging/") or ".." in path_rel:
                n_err += 1
                continue
            if key not in PARSERS:
                n_err += 1
                continue
            try:
                up, _created, _dup = ingest(
                    key, default_storage.path(path_rel), flow=flow,
                    user=request.user, toko=active, provider=provider,
                    password=(passwords[i] if i < len(passwords) else ""),
                )
                if up.source_type.key in MONEY_SOURCES:
                    money_uploads.append(up)
                n_ok += 1
            except Exception as e:  # noqa: BLE001 - tampilkan error parse ke user
                messages.error(request, f"{path_rel}: {e}")
                n_err += 1
            finally:
                if default_storage.exists(path_rel):
                    default_storage.delete(path_rel)
        messages.success(request, f"{n_ok} file diproses, {n_err} gagal.")
        # Auto re-match: mutasi susulan langsung dipasangkan ke batch lama yang
        # punya baris tidak_cocok (ekor malam T+1 / statement bulanan) — tanpa klik.
        # Hasil sukses di-stash ke session (pola PRG) → dirender sekali sebagai
        # kartu penyembuhan; error tetap lewat flash.
        healing = _auto_rematch(active, money_uploads, user=request.user)
        for h in healing:
            if h["level"] == "error":
                messages.error(request, f"Re-match otomatis Batch #{h['batch_no']} gagal: {h['error']}")
        sukses = [h for h in healing if h["level"] == "success"]
        if sukses:
            request.session["healing_report"] = sukses
        return redirect("upload")
    if request.method == "POST" and request.POST.get("action") == "analyze":
        _sweep_staging()
        preview = []
        dilewati = 0
        berkas = request.FILES.getlist("files")
        total = sum(f.size for f in berkas)
        if total > _REQ_MAX_BYTES:
            messages.error(
                request,
                f"Total upload terlalu besar ({total // (1024 * 1024)}MB) — "
                f"maksimal {_REQ_MAX_BYTES // (1024 * 1024)}MB per sekali analisis.",
            )
            berkas = []
        for f in berkas:
            if f.size > _FILE_MAX_BYTES:
                messages.error(
                    request,
                    f"{f.name}: terlalu besar ({f.size // (1024 * 1024)}MB) — "
                    f"maksimal {_FILE_MAX_BYTES // (1024 * 1024)}MB per file.",
                )
                continue
            if f.name.lower().endswith(".zip"):
                isi, n_skip, err = _extract_zip(f)
                dilewati += n_skip
                if err:
                    messages.error(request, f"{f.name}: {err}")
                    continue
                for nama, data in isi:
                    preview.append(_analyze_file(nama, ContentFile(data)))
                continue
            if _is_junk_name(f.name):
                dilewati += 1
                continue
            preview.append(_analyze_file(f.name, f))
        if dilewati:
            messages.info(
                request,
                f"{dilewati} file dilewati (tersembunyi / bukan xlsx·xls·csv·pdf).",
            )
        saran = _saran_tanggal(active)
        return render(request, "web/upload.html", {
            "preview": preview, "parsers": sorted(PARSERS.keys()),
            "flows": ["", "dp", "wd"], "active_toko": active,
            "n_siap": sum(1 for p in preview if not p["needs_confirm"] and not p["needs_password"]),
            "n_cek": sum(1 for p in preview if p["needs_confirm"]),
            "n_pwd": sum(1 for p in preview if p["needs_password"]),
            "uploads": _uploads_for(active),
            "healing": request.session.pop("healing_report", None),
            "saran": saran,
            "saran_label": _window_label(saran, saran, with_year=True) if saran else "",
        })
    saran = _saran_tanggal(active)
    return render(request, "web/upload.html", {
        "parsers": sorted(PARSERS.keys()), "active_toko": active,
        "uploads": _uploads_for(active),
        "healing": request.session.pop("healing_report", None),
        "saran": saran,
        "saran_label": _window_label(saran, saran, with_year=True) if saran else "",
    })


@login_required
def transactions(request):
    active = _active_toko(request)
    if active is None:
        return render(request, "web/no_toko.html")
    qs = (
        Transaction.objects.filter(toko=active)
        .select_related("source_type", "account", "upload", "upload__account")
        .order_by("-occurred_at")
    )
    src = request.GET.get("source", "")
    jenis = request.GET.get("jenis", "")
    q = request.GET.get("q", "").strip()
    bank = request.GET.get("bank", "").strip()
    if src:
        qs = qs.filter(source_type__key=src)
    if jenis:
        qs = qs.filter(jenis=jenis)
    if q:
        cond = (
            Q(username__icontains=q)
            | Q(ticket_no__icontains=q)
            | Q(reference__icontains=q)
            | Q(counterparty__icontains=q)
        )
        # Angka (boleh berformat "50.000" / "50,000") → cari juga nominal persis.
        digits = q.replace(".", "").replace(",", "")
        if digits.isdigit():
            cond |= Q(amount=digits)
        qs = qs.filter(cond)
    tgl_dari = request.GET.get("date_from", "").strip()
    tgl_sampai = request.GET.get("date_to", "").strip()
    if tgl_dari:
        qs = qs.filter(occurred_at__date__gte=tgl_dari)
    if tgl_sampai:
        qs = qs.filter(occurred_at__date__lte=tgl_sampai)

    # Tombol filter per-bank: label diturunkan dari data upload toko ini
    # (account.provider / provider / nama file) — bukan daftar hardcode.
    bank_options = []
    if src in ("bank", "gateway"):
        ups = Upload.objects.filter(toko=active, source_type__key=src).select_related("account")
        label_by_upload = {
            u.id: specific_source_label(src, account=u.account, upload=u) for u in ups
        }
        fallback = src.capitalize()
        bank_options = sorted({lbl for lbl in label_by_upload.values() if lbl and lbl != fallback})
        if bank:
            qs = qs.filter(
                upload_id__in=[uid for uid, lbl in label_by_upload.items() if lbl == bank]
            )
    else:
        bank = ""

    page = Paginator(qs, 40).get_page(request.GET.get("page"))

    # Ticket/Username/Nama Lengkap sisi uang: ambil dari pasangan panel/bracket
    # hasil rekonsiliasi — hanya untuk baris halaman ini (tanpa join tabel penuh).
    txs = list(page.object_list)
    page.object_list = txs
    money_ids = [t.id for t in txs if t.source_type.key in ("bank", "gateway")]
    best = {}
    if money_ids:
        results = (
            MatchResult.objects.filter(right_id__in=money_ids, left__isnull=False)
            .exclude(bucket=MatchResult.Bucket.TIDAK)
            .select_related("left")
        )
        for r in results:
            # cocok > skor tertinggi > run terbaru
            rank = (r.bucket == MatchResult.Bucket.COCOK, r.score or 0, r.run_id, r.id)
            if r.right_id not in best or rank > best[r.right_id][0]:
                best[r.right_id] = (rank, r.left)
    for t in txs:
        t.is_money = t.source_type.key in ("bank", "gateway")
        t.matched_panel = best.get(t.id, (None, None))[1]

    ctx = {
        "page": page,
        "sources": SourceType.objects.all(),
        "jenis_choices": Transaction.Jenis.choices,
        "src": src,
        "jenis": jenis,
        "q": q,
        "bank": bank,
        "bank_options": bank_options,
        "total": page.paginator.count,
        "date_from": tgl_dari,
        "date_to": tgl_sampai,
    }
    return render(request, "web/transactions.html", ctx)


# Nama bulan pendek Indonesia — filter |date Django ikut LANGUAGE_CODE (en) dan
# ganti locale global terlalu invasif untuk satu label.
_BULAN_ID = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def _tgl_id(d, with_year=False):
    s = f"{d.day} {_BULAN_ID[d.month - 1]}"
    return f"{s} {d.year}" if with_year else s


def _window_label(dfrom, dto, with_year=False):
    """Label tanggal DATA sebuah batch — identitas utama batch di mata auditor
    ("hari 28"), bukan waktu dibuatnya. '28 Jun' / '27–29 Jun' /
    '30 Jun – 2 Jul' / 'semua tanggal'."""
    if not dfrom and not dto:
        return "semua tanggal"
    if dfrom and dto:
        if dfrom == dto:
            return _tgl_id(dfrom, with_year)
        if (dfrom.year, dfrom.month) == (dto.year, dto.month):
            return f"{dfrom.day}–{_tgl_id(dto, with_year)}"
        return f"{_tgl_id(dfrom)} – {_tgl_id(dto, with_year)}"
    return f"sejak {_tgl_id(dfrom, with_year)}" if dfrom else f"s.d. {_tgl_id(dto, with_year)}"


def _status_batches(batches):
    """Chip status per batch riwayat (F3): pisahkan ekor T+1 yang normal dari
    selisih beneran, dan tandai batch cangkang yang buktinya sudah dihapus.
    Mengisi atribut `window_label` dan `chip` pada tiap batch (in-place —
    JANGAN pakai nama `status`: itu field model utk background run)."""
    ids = [b.id for b in batches]
    res_counts = {
        row["run__batch"]: row["n"]
        for row in MatchResult.objects.filter(run__batch__in=ids)
        .values("run__batch").annotate(n=Count("id"))
    }
    for b in batches:
        b.window_label = _window_label(b.date_from, b.date_to)
        tidak, tinjau = b.bk["tidak_cocok"], b.bk["perlu_tinjau"]
        ekor = 0
        if tidak and b.date_to:
            ekor = min(tidak, MatchResult.objects.filter(
                run__batch=b, bucket=MatchResult.Bucket.TIDAK, reason_code="no_money",
                left__occurred_at__date=b.date_to, left__occurred_at__hour__gte=_JAM_EKOR,
            ).count())
        b.chip = {
            "rusak": b.bk["total"] > 0 and res_counts.get(b.id, 0) == 0
                     and b.status == ReconBatch.Status.SELESAI,
            "ekor": ekor,
            "real": max(tidak - ekor, 0),
            "tinjau": tinjau,
            "final": tidak == 0 and tinjau == 0 and b.status == ReconBatch.Status.SELESAI,
            "berjalan": b.status == ReconBatch.Status.BERJALAN,
            "gagal": b.status == ReconBatch.Status.GAGAL,
        }


def _rentang_sumber(toko):
    """Rentang tanggal data AKTIF per sumber (kunci = kunci kelengkapan engine).
    Dipakai kartu kelengkapan: sumber yang 'kosong' di window terpilih diberi
    petunjuk di tanggal mana dia PUNYA data — biar user tidak mengira upload
    hilang padahal cuma windownya yang tidak pas."""
    base = Transaction.objects.filter(
        toko=toko, is_duplicate=False, consumed_by_batch__isnull=True
    )
    spec = {
        "panel_dp": {"source_type__key": "panel", "jenis": "depo"},
        "panel_wd": {"source_type__key": "panel", "jenis": "wd"},
        "bracket": {"source_type__key": "bracket"},
        "bank": {"source_type__key": "bank"},
        "gateway": {"source_type__key": "gateway"},
    }
    out = {}
    for key, f in spec.items():
        agg = base.filter(**f).aggregate(lo=Min("occurred_at"), hi=Max("occurred_at"))
        if agg["lo"]:
            out[key] = _window_label(agg["lo"].date(), agg["hi"].date())
    return out


# Di atas ambang baris aktif ini, reconcile jalan di THREAD background —
# request kembali seketika, worker gunicorn tidak dibunuh timeout 120s.
_BG_THRESHOLD = 20000


def _perkiraan_baris(toko, date_from, date_to):
    """Perkiraan kasar beban run: transaksi AKTIF toko dalam window (tanpa
    pelebaran T+1 — cukup akurat untuk keputusan sync vs background)."""
    qs = Transaction.objects.filter(
        toko=toko, is_duplicate=False, consumed_by_batch__isnull=True
    )
    if date_from:
        qs = qs.filter(occurred_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(occurred_at__date__lte=date_to)
    return qs.count()


def _spawn_bg(batch_pk):
    """Jalankan run_batch_into di thread daemon. Koneksi DB thread ditutup di
    akhir; error sudah ditandai run_batch_into (status=gagal + error_note)."""
    import logging
    import threading

    from django.db import connections

    def job():
        try:
            batch = ReconBatch.objects.get(pk=batch_pk)
            run_batch_into(batch)
        except Exception:  # noqa: BLE001 - status gagal sudah tercatat di batch
            logging.getLogger(__name__).exception(
                "reconcile background gagal (batch %s)", batch_pk
            )
        finally:
            connections.close_all()

    threading.Thread(target=job, daemon=True).start()


@login_required
def reconcile(request):
    active = _active_toko(request)
    if active is None:
        return render(request, "web/no_toko.html")
    if request.method == "POST":
        tol = get_object_or_404(ToleranceProfile, name=request.POST.get("tolerance", "Default"))
        # Checkbox inc_* per baris kelengkapan = sumber yang DIIKUTKAN. Tidak ada
        # → tidak dicentang → tidak dicocokkan & tidak dikonsumsi.
        include = {
            "panel_dp": "inc_panel_dp" in request.POST,
            "panel_wd": "inc_panel_wd" in request.POST,
            "bracket": "inc_bracket" in request.POST,
            "bank": "inc_bank" in request.POST,
            "gateway": "inc_gateway" in request.POST,
        }
        df_post = request.POST.get("date_from") or None
        dt_post = request.POST.get("date_to") or None
        if _perkiraan_baris(active, df_post, dt_post) > _BG_THRESHOLD:
            batch = ReconBatch.objects.create(
                toko=active, tolerance=tol, date_from=df_post, date_to=dt_post,
                created_by=request.user, include=include,
                status=ReconBatch.Status.BERJALAN,
            )
            _spawn_bg(batch.pk)
            no = ReconBatch.objects.filter(toko=active).count()
            catat(
                request.user, "reconcile", f"Batch #{no}", toko=active,
                batch_pk=batch.pk, date_from=df_post or "", date_to=dt_post or "",
                latar_belakang=True,
            )
            messages.info(
                request,
                f"Data besar — rekonsiliasi Batch #{no} berjalan di latar belakang. "
                f"Halaman batch menyegarkan otomatis sampai selesai.",
            )
            return redirect("batch_detail", pk=batch.pk)
        batch = run_batch(
            active, tol,
            df_post,
            dt_post,
            user=request.user,
            include=include,
        )
        no = ReconBatch.objects.filter(toko=active).count()
        catat(
            request.user, "reconcile", f"Batch #{no}", toko=active,
            batch_pk=batch.pk,
            date_from=request.POST.get("date_from") or "",
            date_to=request.POST.get("date_to") or "",
        )
        messages.success(request, f"Rekonsiliasi selesai (Batch #{no}).")
        return redirect("batch_detail", pk=batch.pk)

    df = request.GET.get("date_from") or None
    dt = request.GET.get("date_to") or None
    # Saran tanggal: hari berikutnya yang belum direkonsiliasi. Tanpa param
    # eksplisit → prefill (cegah footgun "tanggal kosong = telan semua data");
    # selalu diteruskan sebagai data-saran utk guard urutan (loncat tanggal =
    # batch baru bisa mengonsumsi uang hari sebelumnya → salah atribusi permanen).
    saran = _saran_tanggal(active)
    tanggal_disarankan = False
    if df is None and dt is None and saran:
        df = dt = saran.isoformat()
        tanggal_disarankan = True
    bank = request.GET.get("bank", "")
    if bank not in ("bank", "gateway"):
        bank = ""  # nilai tak dikenal → perlakukan sebagai "semua sumber"
    # Nomor dihitung dari SEMUA batch toko dulu (posisi asli), BARU difilter —
    # supaya nomor batch tidak berubah saat filter sumber uang aktif.
    all_batches = list(ReconBatch.objects.filter(toko=active).order_by("-id"))
    total = len(all_batches)
    for i, b in enumerate(all_batches):
        b.no = total - i
        # Triple bucket ternormalisasi utk bucket-bar (summary lama bisa tanpa buckets).
        bk = (b.summary or {}).get("buckets") or {}
        b.bk = {
            "cocok": bk.get("cocok") or 0,
            "perlu_tinjau": bk.get("perlu_tinjau") or 0,
            "tidak_cocok": bk.get("tidak_cocok") or 0,
        }
        b.bk["total"] = b.bk["cocok"] + b.bk["perlu_tinjau"] + b.bk["tidak_cocok"]
    if bank:
        all_batches = [b for b in all_batches if (b.completeness or {}).get(bank)]
    batches = all_batches[:20]
    _status_batches(batches)  # chip status + label tanggal data, hanya utk yg tampil
    # Konteks tanggal kartu kelengkapan: label window + rentang data aktif per
    # sumber (buat baris "kosong" — kosong DI TANGGAL INI, bukan datanya hilang).
    kelengkapan_label = ""
    try:
        if df and dt:
            kelengkapan_label = _window_label(date.fromisoformat(df), date.fromisoformat(dt))
    except ValueError:
        pass
    ctx = {
        "active_toko": active,
        "completeness": check_completeness(active, df, dt),
        "tolerances": ToleranceProfile.objects.all(),
        "batches": batches,
        "bank": bank,
        "date_from": df or "", "date_to": dt or "",
        "tanggal_disarankan": tanggal_disarankan,
        "saran_iso": saran.isoformat() if saran else "",
        "kelengkapan_label": kelengkapan_label,
        "rentang_sumber": _rentang_sumber(active),
    }
    return render(request, "web/reconcile.html", ctx)


# Jam mulai "malam" untuk heuristik ekor T+1: transaksi >= jam ini di hari
# terakhir window kemungkinan settle besok (file mutasinya belum terupload).
_JAM_EKOR = 17


def _pending_t1(result, date_to):
    """True bila baris tidak_cocok ini kemungkinan EKOR T+1 (bukan selisih nyata):
    no_money + transaksi malam di hari terakhir window → uangnya baru datang di
    file besok, tertutup auto re-match."""
    return bool(
        date_to
        and result.bucket == MatchResult.Bucket.TIDAK
        and result.reason_code == "no_money"
        and result.left_id
        and result.left.occurred_at.date() == date_to
        and result.left.occurred_at.hour >= _JAM_EKOR
    )


@login_required
def batch_detail(request, pk):
    batch = get_object_or_404(ReconBatch, pk=pk, toko__in=tokos_for(request.user))
    batch_no = ReconBatch.objects.filter(toko=batch.toko, id__lte=batch.id).count()
    s = batch.summary or {}
    # Triple bucket ternormalisasi utk bucket-bar (summary lama bisa tanpa buckets).
    raw_bk = s.get("buckets") or {}
    bk = {
        "cocok": raw_bk.get("cocok") or 0,
        "perlu_tinjau": raw_bk.get("perlu_tinjau") or 0,
        "tidak_cocok": raw_bk.get("tidak_cocok") or 0,
    }
    bk["total"] = bk["cocok"] + bk["perlu_tinjau"] + bk["tidak_cocok"]
    # Deteksi cangkang: summary bilang ada hasil tapi MatchResult-nya sudah hilang
    # (upload sumber di-delete → tx cascade → MatchResult cascade, summary basi).
    rusak = bk["total"] > 0 and not MatchResult.objects.filter(run__batch=batch).exists()
    # Ekor T+1 dalam RUPIAH per arah (F5) — pertanyaan pagi auditor bukan
    # "berapa baris" tapi "berapa rupiah yang tinggal nunggu file besok, berapa
    # yang beneran harus dikejar". sisa = selisih summary − rupiah ekor.
    pending_t1 = None
    if batch.date_to:
        agg = {
            row["left__jenis"]: row
            for row in MatchResult.objects.filter(
                run__batch=batch, bucket=MatchResult.Bucket.TIDAK, reason_code="no_money",
                left__occurred_at__date=batch.date_to, left__occurred_at__hour__gte=_JAM_EKOR,
            ).values("left__jenis").annotate(n=Count("id"), tot=Sum("left__amount"))
        }
        n = sum(row["n"] for row in agg.values())
        if n:
            dp_amt = float(agg.get("dp", {}).get("tot") or 0)
            wd_amt = float(agg.get("wd", {}).get("tot") or 0)
            pending_t1 = {
                "n": n, "dp": dp_amt, "wd": wd_amt,
                "dp_sisa": max(((s.get("dp") or {}).get("selisih") or 0) - dp_amt, 0),
                "wd_sisa": max(((s.get("wd") or {}).get("selisih") or 0) - wd_amt, 0),
            }
    # Riwayat aksi batch ini (reconcile/re-match/review/…) — jejak akuntabilitas.
    riwayat_aksi = (
        AuditLog.objects.filter(detail__batch_pk=batch.pk)
        .select_related("user")[:20]
    )
    return render(request, "web/batch_detail.html", {
        "batch": batch, "batch_no": batch_no, "s": s, "bk": bk, "runs": batch.runs.all(),
        "healing": request.session.pop("healing_report", None),
        "pending_t1": pending_t1,
        "window_label": _window_label(batch.date_from, batch.date_to, with_year=True),
        "rusak": rusak,
        "riwayat_aksi": riwayat_aksi,
    })


@login_required
@require_POST
def rematch(request, pk):
    """Re-match batch: pasangkan mutasi uang susulan ke baris tidak_cocok batch ini
    (ekor malam T+1 / statement BNI bulanan) — tanpa hapus batch."""
    batch = get_object_or_404(ReconBatch, pk=pk, toko__in=tokos_for(request.user))
    before = _selisih_abs(batch)
    stats = rematch_batch(batch, user=request.user)
    catat(
        request.user, "rematch", f"Batch #{pk}", toko=batch.toko,
        batch_pk=batch.pk, terpasang=stats["terpasang"],
    )
    if stats["terpasang"]:
        batch.refresh_from_db()
        no = ReconBatch.objects.filter(toko=batch.toko, id__lte=batch.id).count()
        # Kartu penyembuhan yang sama dgn auto re-match — stash session, render sekali.
        request.session["healing_report"] = [{
            "level": "success", "batch_pk": batch.pk, "batch_no": no,
            "terpasang": stats["terpasang"], "cocok": stats["cocok"],
            "perlu_tinjau": stats["perlu_tinjau"],
            "selisih_before": before, "selisih_after": _selisih_abs(batch),
        }]
    else:
        messages.info(request, "Tidak ada baris baru yang bisa dipasangkan.")
    return redirect("batch_detail", pk=batch.pk)


@login_required
def run_detail(request, pk):
    import collections

    from django.db.models.fields.json import KeyTextTransform

    run = get_object_or_404(MatchRun, pk=pk, batch__toko__in=tokos_for(request.user))
    qs = MatchResult.objects.filter(run=run).select_related("left", "right").order_by("bucket", "-score")
    bucket = request.GET.get("bucket", "")
    if bucket:
        qs = qs.filter(bucket=bucket)

    # Daftar chip channel/sumber dibangun dari qs SETELAH filter bucket tapi SEBELUM
    # filter channel — segmen pertama Player Bank sisi Panel ("DANA|nama|nomor").
    # Kunci JSON "Player Bank" mengandung spasi → tak bisa jadi alias values_list
    # langsung, jadi anotasi lewat KeyTextTransform dgn alias aman (jalan di
    # sqlite JSON1 & Postgres JSONB).
    counter = collections.Counter()
    pb_values = qs.annotate(
        _player_bank=KeyTextTransform("Player Bank", "left__raw")
    ).values_list("_player_bank", flat=True)
    for v in pb_values:
        if not v:
            continue
        name = str(v).split("|")[0].strip().upper()
        if name:
            counter[name] += 1
    channels = counter.most_common()  # [(nama, count), ...] urut count desc

    # Filter channel diterapkan SETELAH bucket; normalisasi ke upper + istartswith
    # supaya kapitalisasi tersimpan tak berpengaruh.
    channel = request.GET.get("channel", "").strip().upper()
    if channel:
        qs = qs.filter(**{"left__raw__Player Bank__istartswith": channel + "|"})

    page = Paginator(qs, 40).get_page(request.GET.get("page"))
    # Tandai kemungkinan ekor T+1 (transaksi malam hari terakhir window) supaya
    # auditor bisa membedakan "menunggu mutasi besok" dari selisih nyata.
    dto = run.date_to or (run.batch.date_to if run.batch else None)
    for r in page:
        r.pending_t1 = _pending_t1(r, dto)
    left_label, right_label = REL_LABELS.get(run.relation, ("Kiri", "Kanan"))
    # Nomor batch per-toko (posisi urut, bukan pk global) — konsisten dgn batch_detail.
    batch = run.batch
    batch_no = (
        ReconBatch.objects.filter(toko=batch.toko, id__lte=batch.id).count() if batch else None
    )
    # Deteksi cangkang: summary run berisi tapi hasil DB-nya sudah tersapu cascade
    # hapus upload → stat kartu tak bisa diverifikasi, kasih banner (bukan disembunyikan).
    rsum = run.summary or {}
    total_summary = sum(rsum.get(k) or 0 for k in ("cocok", "perlu_tinjau", "tidak_cocok"))
    rusak = total_summary > 0 and not run.results.exists()
    ctx = {
        "run": run, "page": page, "bucket": bucket, "bucket_meta": BUCKET_META,
        "left_label": left_label, "right_label": right_label,
        "batch": batch, "batch_no": batch_no,
        "channels": channels, "channel": channel,
        "rusak": rusak,
    }
    # Request htmx (filter tab / pager) → cukup fragmen tabel, tanpa shell.
    if request.headers.get("HX-Request"):
        return render(request, "web/_run_table.html", ctx)
    return render(request, "web/run_detail.html", ctx)


def _refresh_bucket_summaries(run):
    """Sinkronkan hitungan bucket run.summary & batch.summary['buckets'] dari DB
    setelah review manual — stat kartu tidak boleh menyimpang dari tabel."""
    c = Counter(run.results.values_list("bucket", flat=True))
    s = run.summary or {}
    s["cocok"] = c.get("cocok", 0)
    s["perlu_tinjau"] = c.get("perlu_tinjau", 0)
    s["tidak_cocok"] = c.get("tidak_cocok", 0)
    run.summary = s
    run.save(update_fields=["summary"])
    if run.batch_id:
        batch = run.batch
        cb = Counter(
            MatchResult.objects.filter(run__batch=batch).values_list("bucket", flat=True)
        )
        bs = batch.summary or {}
        buckets = bs.get("buckets", {})
        buckets["cocok"] = cb.get("cocok", 0)
        buckets["perlu_tinjau"] = cb.get("perlu_tinjau", 0)
        buckets["tidak_cocok"] = cb.get("tidak_cocok", 0)
        bs["buckets"] = buckets
        batch.summary = bs
        batch.save(update_fields=["summary"])


def _bebaskan_konsumsi(tx, batch_id):
    """Bebaskan consumed_by_batch baris uang yang pasangannya DITOLAK auditor,
    supaya kembali ke pool aktif (batch lain / re-match bisa memakainya).
    Hanya bila yang mengonsumsi = batch pemilik run DAN baris tak dipakai
    MatchResult lain — bukti lain / konsumsi batch lain tetap mengunci.
    Kembalikan pk transaksi yang dibebaskan, atau None."""
    if tx is None or batch_id is None or tx.consumed_by_batch_id != batch_id:
        return None
    if MatchResult.objects.filter(Q(left=tx) | Q(right=tx)).exists():
        return None
    tx.consumed_by_batch = None
    tx.save(update_fields=["consumed_by_batch"])
    return tx.pk


@login_required
@require_POST
def review(request, pk):
    r = get_object_or_404(MatchResult, pk=pk, run__batch__toko__in=tokos_for(request.user))
    action = request.POST.get("action", "")
    reason = request.POST.get("reason", "")
    buckets = {
        "mark_matched": MatchResult.Bucket.COCOK,
        "mark_review": MatchResult.Bucket.TINJAU,
        "mark_unmatched": MatchResult.Bucket.TIDAK,
    }
    if action not in buckets:
        return HttpResponseBadRequest("Aksi tidak dikenal.")
    r.bucket = buckets[action]
    r.reason_code = "manual_override"
    fields = ["bucket", "reason_code"]
    # Menolak pasangan = sisi uang (right) DILEPAS — kalau tidak, uangnya tetap
    # terhitung matched (_matched_money pakai left+right, bukan bucket) dan
    # konsumsinya tak pernah bisa dibebaskan.
    uang = r.right if action == "mark_unmatched" else None
    if uang is not None:
        r.right = None
        fields.append("right")
    r.save(update_fields=fields)
    uang_pk = _bebaskan_konsumsi(uang, r.run.batch_id) if uang is not None else None
    ReviewAction.objects.create(result=r, action=action, reason=reason, reviewer=request.user)
    catat(
        request.user, "review", f"Hasil #{r.pk} → {action}",
        toko=r.run.batch.toko if r.run.batch_id else None,
        result_pk=r.pk, action=action, batch_pk=r.run.batch_id,
        uang_dibebaskan=uang_pk,
    )
    _refresh_bucket_summaries(r.run)
    return render(request, "web/_result_row.html", {"r": r, "bucket_meta": BUCKET_META})


@login_required
@require_POST
def review_bulk(request):
    """Review MASSAL — semantik sama persis dengan review per-baris (bucket +
    reason_code=manual_override + jejak ReviewAction per baris), untuk banyak
    hasil sekaligus: ratusan weak_name/hari tak mungkin diklik satu-satu."""
    action = request.POST.get("action", "")
    buckets = {
        "mark_matched": MatchResult.Bucket.COCOK,
        "mark_review": MatchResult.Bucket.TINJAU,
        "mark_unmatched": MatchResult.Bucket.TIDAK,
    }
    if action not in buckets:
        return HttpResponseBadRequest("Aksi tidak dikenal.")
    ids = request.POST.getlist("result_ids")
    results = list(
        MatchResult.objects.filter(pk__in=ids, run__batch__toko__in=tokos_for(request.user))
        .select_related("right", "run")
    )
    # Pasangan yang ditolak: lepas right dulu (bulk), BARU cek konsumsi — dua
    # target duplikat yang menunjuk uang sama tetap bebas kalau dua-duanya ditolak.
    uang_kandidat = {}
    for r in results:
        r.bucket = buckets[action]
        r.reason_code = "manual_override"
        if action == "mark_unmatched" and r.right_id is not None:
            uang_kandidat.setdefault(r.right_id, (r.right, r.run.batch_id))
            r.right = None
    fields = ["bucket", "reason_code"]
    if action == "mark_unmatched":
        fields.append("right")
    MatchResult.objects.bulk_update(results, fields, batch_size=500)
    n_bebas = sum(
        1 for tx, batch_id in uang_kandidat.values()
        if _bebaskan_konsumsi(tx, batch_id) is not None
    )
    ReviewAction.objects.bulk_create(
        [ReviewAction(result=r, action=action, reason="review massal", reviewer=request.user)
         for r in results],
        batch_size=500,
    )
    runs_terdampak = {r.run_id: r.run for r in results}
    for run in runs_terdampak.values():
        _refresh_bucket_summaries(run)
    if results:
        first_run = results[0].run
        catat(
            request.user, "review_massal", f"{len(results)} baris → {action}",
            toko=first_run.batch.toko if first_run.batch_id else None,
            n=len(results), action=action, batch_pk=first_run.batch_id,
            uang_dibebaskan=n_bebas,
        )
    label = {"mark_matched": "cocok", "mark_review": "perlu ditinjau",
             "mark_unmatched": "tidak cocok"}[action]
    messages.success(request, f"{len(results)} baris ditandai {label}.")
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return redirect(next_url)
    if results:
        return redirect("run_detail", pk=results[0].run_id)
    return redirect("reconcile")


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
    L, R = REL_LABELS.get(run.relation, ("Kiri", "Kanan"))
    headers = ["Bucket", f"{L} Ticket", f"{L} Amount", f"{L} User", f"{L} Nama Lengkap",
               f"{L} Player Bank", f"{L} Bank Title", f"{L} Handler", f"{L} Waktu",
               R, f"{R} Sumber", f"{R} Amount", f"{R} Waktu", "Skor", "Alasan", "Detail"]
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
            left.counterparty if left else "",
            (left.raw or {}).get("Player Bank", "") if left else "",
            (left.raw or {}).get("Bank Title", "") if left else "",
            (left.raw or {}).get("Handler", "") if left else "",
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
