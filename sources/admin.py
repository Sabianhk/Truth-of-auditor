from django.contrib import admin

from reconciliation.admin import NoDeleteAdmin

from .models import Account, ColumnTemplate, SourceType, Toko, Upload


@admin.register(Toko)
class TokoAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "key")


@admin.register(SourceType)
class SourceTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "is_money_source")


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
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
class UploadAdmin(NoDeleteAdmin):
    # Hapus hanya lewat UI aplikasi (guard _locking_batches + audit trail);
    # delete admin membypass guard integritas → bukti rekonsiliasi lenyap.
    list_display = (
        "original_name", "source_type", "account", "flow", "recon_date",
        "status", "rows_parsed", "rows_duplicate", "created_at",
    )
    list_filter = ("source_type", "status", "flow")
    search_fields = ("original_name",)
    date_hierarchy = "recon_date"
    # M2M link duplikat bisa ratusan ribu pilihan — jangan render sebagai widget form.
    exclude = ("duplicate_transactions",)
