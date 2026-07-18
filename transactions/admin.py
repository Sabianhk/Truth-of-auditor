from django.contrib import admin

from reconciliation.admin import NoDeleteAdmin

from .models import Transaction


@admin.register(Transaction)
class TransactionAdmin(NoDeleteAdmin):
    # Hapus hanya lewat UI aplikasi (hapus upload/batch dgn guard integritas);
    # delete admin membypass guard + M2M duplikat lintas upload.
    list_display = (
        "occurred_at", "source_type", "jenis", "amount",
        "username", "ticket_no", "reference", "is_duplicate",
    )
    list_filter = ("source_type", "jenis", "is_duplicate", "account")
    search_fields = ("username", "ticket_no", "reference", "counterparty", "description")
    date_hierarchy = "occurred_at"
    list_select_related = ("source_type", "account")
