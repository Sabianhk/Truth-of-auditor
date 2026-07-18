from django.db import models

from core.models import TimeStampedModel


class ToleranceProfile(TimeStampedModel):
    """Parameter toleransi pencocokan (bisa diedit & dipakai ulang)."""

    name = models.CharField(max_length=100, unique=True)
    date_window_days = models.IntegerField(default=1)
    date_direction = models.CharField(
        max_length=30,
        default="target_after_base",
        help_text=(
            "BELUM dipakai matcher mana pun — arah tanggal saat ini tertanam "
            "di engine (sisi uang >= sisi kredit). Mengubah field ini tidak "
            "berpengaruh."
        ),
    )
    amount_abs_tol = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    amount_pct_tol = models.DecimalField(
        max_digits=6, decimal_places=4, default=0,
        help_text=(
            "Saat ini hanya dipakai relasi Panel↔Bracket (amount_ok) — "
            "BELUM dipakai matcher uang (Panel/Bracket↔Bank)."
        ),
    )
    fuzzy_threshold = models.IntegerField(default=85)

    def __str__(self):
        return self.name


class MatchRun(TimeStampedModel):
    class Relation(models.TextChoices):
        PANEL_BRACKET = "panel_bracket", "Panel ↔ Bracket"
        PANEL_BANK = "panel_bank", "Panel ↔ Mutasi Bank"
        # CLI-only (`manage.py match bracket_bank`) — tidak dipakai alur web.
        BRACKET_BANK = "bracket_bank", "Bracket ↔ Mutasi Bank"
        # Belum ada matcher-nya sama sekali (CLI menolak); placeholder rencana.
        SALDO = "saldo", "Rekonsiliasi Saldo"

    relation = models.CharField(max_length=20, choices=Relation.choices)
    tolerance = models.ForeignKey(ToleranceProfile, on_delete=models.PROTECT)
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    params = models.JSONField(default=dict)
    summary = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True
    )
    batch = models.ForeignKey(
        "ReconBatch", on_delete=models.CASCADE, null=True, blank=True, related_name="runs"
    )

    def __str__(self):
        return f"{self.get_relation_display()} #{self.pk}"


class ReconBatch(TimeStampedModel):
    """Satu sesi rekonsiliasi paralel untuk satu Toko + periode."""

    toko = models.ForeignKey("sources.Toko", on_delete=models.PROTECT, null=True, blank=True)
    tolerance = models.ForeignKey(ToleranceProfile, on_delete=models.PROTECT)
    recon_date = models.DateField(
        null=True, blank=True, db_index=True,
        help_text="Tanggal rekonsiliasi harian — satu batch per (toko, tanggal)",
    )
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    summary = models.JSONField(default=dict)
    completeness = models.JSONField(default=dict)
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["toko", "recon_date"],
                condition=models.Q(recon_date__isnull=False),
                name="uniq_reconbatch_toko_recon_date",
            )
        ]

    def __str__(self):
        return f"Batch #{self.pk}"


class MatchResult(TimeStampedModel):
    class Bucket(models.TextChoices):
        COCOK = "cocok", "Cocok"
        TIDAK = "tidak_cocok", "Tidak Cocok"
        TINJAU = "perlu_tinjau", "Perlu Ditinjau"

    run = models.ForeignKey(MatchRun, on_delete=models.CASCADE, related_name="results")
    bucket = models.CharField(max_length=15, choices=Bucket.choices)
    left = models.ForeignKey(
        "transactions.Transaction",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    right = models.ForeignKey(
        "transactions.Transaction",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    score = models.FloatField(default=0)
    reason_code = models.CharField(max_length=50, blank=True)
    reason_detail = models.TextField(blank=True)
    resolved_by_batch = models.ForeignKey(
        "ReconBatch", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="resolved_results",
        help_text="Batch yang men-settle hasil tidak_cocok/no_money ini terlambat",
    )

    class Meta:
        # Difilter per bucket/reason_code hampir tiap request (context
        # processor antrean tinjau, _carried_qs, review_queue) — tanpa index
        # = seq-scan tabel hasil terbesar.
        indexes = [
            models.Index(fields=["bucket"]),
            models.Index(fields=["reason_code", "bucket"]),
        ]

    def __str__(self):
        return f"{self.bucket} ({self.reason_code})"


class ReviewAction(TimeStampedModel):
    """Jejak override manual auditor."""

    result = models.ForeignKey(
        MatchResult, on_delete=models.CASCADE, related_name="reviews"
    )
    action = models.CharField(max_length=30)
    reason = models.TextField(blank=True)
    reviewer = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True
    )

    def __str__(self):
        return f"{self.action} on #{self.result_id}"
