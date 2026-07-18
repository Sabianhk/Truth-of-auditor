from django.db import models

from core.models import TimeStampedModel


class SourceType(models.Model):
    """Jenis sumber data: panel / bracket / bank / gateway (extensible)."""

    PANEL, BRACKET, BANK, GATEWAY = "panel", "bracket", "bank", "gateway"
    KIND_CHOICES = [
        (PANEL, "Panel"),
        (BRACKET, "Bracket"),
        (BANK, "Bank"),
        (GATEWAY, "Gateway"),
    ]

    key = models.CharField(max_length=20, unique=True, choices=KIND_CHOICES)
    name = models.CharField(max_length=100)
    is_money_source = models.BooleanField(
        default=False, help_text="Bank & gateway = sumber kebenaran uang"
    )

    def __str__(self):
        return self.name


class Toko(TimeStampedModel):
    """Merek/situs operator (mis. LBS, SLO). Data dipisah per toko."""

    key = models.SlugField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Account(TimeStampedModel):
    """Rekening uang: rekening bank atau gateway pembayaran (multi-rekening)."""

    BANK, GATEWAY = "bank", "gateway"
    KIND_CHOICES = [(BANK, "Bank"), (GATEWAY, "Gateway")]
    DP, WD, BOTH = "dp", "wd", "both"
    FLOW_CHOICES = [(DP, "Deposit"), (WD, "Withdraw"), (BOTH, "Both")]

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    provider = models.CharField(
        max_length=50, help_text="BRI / BCA / MANDIRI / NXPAY / QRFLYER"
    )
    name = models.CharField(max_length=100, help_text="Label/pemilik, mis. 'BCA HENDI'")
    account_no = models.CharField(max_length=64, blank=True)
    flow = models.CharField(max_length=5, choices=FLOW_CHOICES, default=BOTH)
    is_active = models.BooleanField(default=True)
    toko = models.ForeignKey(
        "sources.Toko", on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        return f"{self.provider} {self.name}".strip()


class ColumnTemplate(TimeStampedModel):
    """Aturan parsing satu format file -> field kanonik (per source/provider)."""

    INTL, ID = "intl", "id"
    NUMBER_FORMAT_CHOICES = [(INTL, "1,000.00 (intl)"), (ID, "1.000,00 (ID)")]

    source_type = models.ForeignKey(
        SourceType, on_delete=models.CASCADE, related_name="templates"
    )
    provider = models.CharField(max_length=50, blank=True)
    name = models.CharField(max_length=100)
    header_row = models.IntegerField(default=1, help_text="Baris (1-based) tempat header")
    mapping = models.JSONField(default=dict, help_text="canonical_field -> nama kolom sumber")
    jenis_resolver = models.JSONField(default=dict, help_text="cara tentukan 'jenis'")
    number_format = models.CharField(
        max_length=5, choices=NUMBER_FORMAT_CHOICES, default=INTL
    )
    date_formats = models.JSONField(default=list, help_text="kandidat format tanggal")
    amount_scale = models.IntegerField(
        default=1, help_text="kalikan amount agar jadi rupiah (Panel=1000)"
    )
    is_default = models.BooleanField(default=False)

    def __str__(self):
        return self.name


class Upload(TimeStampedModel):
    """Satu file yang di-upload untuk diparse."""

    UPLOADED, PARSED, ERROR = "uploaded", "parsed", "error"
    STATUS_CHOICES = [(UPLOADED, "Uploaded"), (PARSED, "Parsed"), (ERROR, "Error")]

    source_type = models.ForeignKey(SourceType, on_delete=models.PROTECT)
    account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True)
    toko = models.ForeignKey(
        "sources.Toko", on_delete=models.PROTECT, null=True, blank=True
    )
    provider = models.CharField(max_length=50, blank=True, help_text="Panel provider, mis. Nexus")
    template = models.ForeignKey(
        ColumnTemplate, on_delete=models.SET_NULL, null=True, blank=True
    )
    flow = models.CharField(max_length=5, blank=True)
    recon_date = models.DateField(null=True, blank=True, help_text="tanggal rekonsiliasi")
    # CATATAN: field ini TIDAK pernah diisi — ingest hanya menyimpan baris
    # hasil parse, file asli dibuang usai parse. Menyimpan file asli sebagai
    # bukti audit = keputusan produk (butuh volume + kebijakan retensi), bukan
    # sekadar mengisi field ini.
    file = models.FileField(upload_to="uploads/%Y/%m/")
    original_name = models.CharField(max_length=255, blank=True)
    owner_name = models.CharField(
        max_length=100, blank=True,
        help_text="Nama pemilik rekening dari header file (mis. HENDI) atau nama file",
    )
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=UPLOADED)
    rows_parsed = models.IntegerField(default=0)
    rows_duplicate = models.IntegerField(default=0)
    # Baris file ini yang di-skip dedup karena SUDAH tercatat lewat upload
    # terdahulu (file ekspor bank sering rolling/tumpang-tindih). Link ini
    # yang membuat "isi file" tetap bisa ditampilkan utuh per-file.
    duplicate_transactions = models.ManyToManyField(
        "transactions.Transaction",
        related_name="duplicated_in_uploads",
        blank=True,
    )
    error = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        return self.original_name or f"Upload #{self.pk}"
