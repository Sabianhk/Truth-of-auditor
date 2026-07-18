from django.contrib import admin

from reconciliation.admin import NoDeleteAdmin, ViewOnlyAdmin

from .models import Account, ColumnTemplate, SourceType, Toko, Upload


@admin.register(Toko)
class TokoAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "key")


@admin.register(SourceType)
class SourceTypeAdmin(NoDeleteAdmin):
    # W6-7b: delete CASCADE ColumnTemplate seed -> parser rusak senyap.
    list_display = ("name", "key", "is_money_source")


@admin.register(Account)
class AccountAdmin(NoDeleteAdmin):
    # W6-7b: delete SET_NULL Transaction.account -> grouping rekening berubah senyap.
    list_display = ("provider", "name", "kind", "flow", "account_no", "is_active")
    list_filter = ("kind", "provider", "flow", "is_active")
    search_fields = ("name", "account_no", "provider")


@admin.register(ColumnTemplate)
class ColumnTemplateAdmin(admin.ModelAdmin):
    list_display = (
        "name", "source_type", "provider", "header_row",
        "number_format", "amount_scale", "is_default",
    )
    list_filter = ("source_type", "number_format", "is_default")


@admin.register(Upload)
class UploadAdmin(ViewOnlyAdmin):
    # Hapus hanya lewat UI aplikasi (guard _locking_batches + audit trail);
    # delete admin membypass guard integritas → bukti rekonsiliasi lenyap.
    # W6-7a: view-only penuh — bukti finansial tak boleh diedit/dibuat manual.
    list_display = (
        "original_name", "source_type", "account", "flow", "recon_date",
        "status", "rows_parsed", "rows_duplicate", "created_at",
    )
    list_filter = ("source_type", "status", "flow")
    search_fields = ("original_name",)
    date_hierarchy = "recon_date"
    # M2M link duplikat bisa ratusan ribu pilihan — jangan render sebagai widget form.
    exclude = ("duplicate_transactions",)
