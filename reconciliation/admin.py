from django.contrib import admin

from .models import MatchResult, MatchRun, ReconBatch, ReviewAction, ToleranceProfile


class NoDeleteAdmin(admin.ModelAdmin):
    """Hapus HANYA lewat UI aplikasi (guard integritas + revert settlement +
    audit trail). Delete bawaan admin membypass semuanya → state korup."""

    def has_delete_permission(self, request, obj=None):
        return False


class _MachineResultAdmin(NoDeleteAdmin):
    """Hasil mesin rekonsiliasi — bukan entri manual: add juga dimatikan."""

    def has_add_permission(self, request):
        return False


@admin.register(ToleranceProfile)
class ToleranceProfileAdmin(admin.ModelAdmin):
    list_display = (
        "name", "date_window_days", "date_direction",
        "amount_abs_tol", "amount_pct_tol", "fuzzy_threshold",
    )


@admin.register(MatchRun)
class MatchRunAdmin(_MachineResultAdmin):
    list_display = ("id", "relation", "tolerance", "date_from", "date_to", "created_at")
    list_filter = ("relation",)


@admin.register(MatchResult)
class MatchResultAdmin(_MachineResultAdmin):
    list_display = ("id", "run", "bucket", "reason_code", "score")
    list_filter = ("bucket", "run__relation")
    search_fields = ("reason_code", "reason_detail")


@admin.register(ReviewAction)
class ReviewActionAdmin(NoDeleteAdmin):
    list_display = ("id", "result", "action", "reviewer", "created_at")


@admin.register(ReconBatch)
class ReconBatchAdmin(_MachineResultAdmin):
    list_display = ("id", "toko", "tolerance", "date_from", "date_to", "created_at")
    list_filter = ("toko",)
