from django.contrib import admin

from .models import MatchResult, MatchRun, ReconBatch, ReviewAction, ToleranceProfile


class NoDeleteAdmin(admin.ModelAdmin):
    """Hapus HANYA lewat UI aplikasi (guard integritas + revert settlement +
    audit trail). Delete bawaan admin membypass semuanya → state korup."""

    def has_delete_permission(self, request, obj=None):
        return False


class ViewOnlyAdmin(NoDeleteAdmin):
    """W6-7a: bukti finansial & hasil mesin — admin hanya untuk MELIHAT.
    Edit/tambah dari admin membypass guard aplikasi + audit trail (catat())."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ToleranceProfile)
class ToleranceProfileAdmin(NoDeleteAdmin):
    # W6-7b: profil dihapus (mis. "Default") → reconcile 404. Edit tetap boleh.
    list_display = (
        "name", "date_window_days", "date_direction",
        "amount_abs_tol", "amount_pct_tol", "fuzzy_threshold",
    )


@admin.register(MatchRun)
class MatchRunAdmin(ViewOnlyAdmin):
    list_display = ("id", "relation", "tolerance", "date_from", "date_to", "created_at")
    list_filter = ("relation",)

    def has_delete_permission(self, request, obj=None):
        # W6-7c: run CLI (tanpa batch) BOLEH dihapus dari admin — satu-satunya
        # jalan membebaskan upload yang dikunci guard "hapus run-nya dulu"
        # (alur web tak punya UI hapus run CLI). Run milik batch tetap terkunci;
        # aksi massal changelist (obj=None) juga tetap mati.
        if obj is None or obj.batch_id is not None:
            return False
        return admin.ModelAdmin.has_delete_permission(self, request, obj)


@admin.register(MatchResult)
class MatchResultAdmin(ViewOnlyAdmin):
    list_display = ("id", "run", "bucket", "reason_code", "score")
    list_filter = ("bucket", "run__relation")
    search_fields = ("reason_code", "reason_detail")

    def has_delete_permission(self, request, obj=None):
        # Ikut nasib run CLI-nya: get_deleted_objects memeriksa permission per
        # objek cascade, jadi hapus run CLI butuh izin di hasil-hasilnya juga.
        if obj is None or obj.run.batch_id is not None:
            return False
        return admin.ModelAdmin.has_delete_permission(self, request, obj)


@admin.register(ReviewAction)
class ReviewActionAdmin(ViewOnlyAdmin):
    list_display = ("id", "result", "action", "reviewer", "created_at")

    def has_delete_permission(self, request, obj=None):
        # Cascade dari hapus run CLI (lihat MatchResultAdmin).
        if obj is None or obj.result.run.batch_id is not None:
            return False
        return admin.ModelAdmin.has_delete_permission(self, request, obj)


@admin.register(ReconBatch)
class ReconBatchAdmin(ViewOnlyAdmin):
    list_display = ("id", "toko", "tolerance", "date_from", "date_to", "created_at")
    list_filter = ("toko",)
