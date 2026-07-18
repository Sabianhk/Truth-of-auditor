"""Audit trail (AuditLog): aksi destruktif & reconcile tercatat siapa-kapan-apa.

Tool auditor tanpa jejak auditor = ironi — hapus batch/upload dan jalannya
rekonsiliasi harus bisa dipertanggungjawabkan belakangan.
"""
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import AuditLog
from reconciliation.models import MatchResult, MatchRun, ReconBatch, ToleranceProfile
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        self.lbs = Toko.objects.get(key="lbs")
        self.tol = ToleranceProfile.objects.get(name="Default")
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.adm = User.objects.create_user("adm", password="pw123456", role="admin")
        self.client.login(username="adm", password="pw123456")
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})

    def _batch_dengan_result(self):
        up = Upload.objects.create(source_type=self.panel, toko=self.lbs)
        tx = Transaction.objects.create(
            upload=up, source_type=self.panel, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 28, 10, 0), row_hash="au1",
        )
        batch = ReconBatch.objects.create(
            toko=self.lbs, tolerance=self.tol,
            date_from=date(2026, 6, 28), date_to=date(2026, 6, 28), summary={},
        )
        run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=batch,
            date_from=date(2026, 6, 28), date_to=date(2026, 6, 28),
        )
        # Hasil BERPASANGAN (guard W1-6a: mark_matched butuh baris uang).
        tx2 = Transaction.objects.create(
            upload=up, source_type=self.bank, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 28, 11, 0), row_hash="au2",
        )
        r = MatchResult.objects.create(
            run=run, bucket=MatchResult.Bucket.TINJAU, left=tx, right=tx2,
            score=80, reason_code="name_partial",
        )
        return batch, run, r


class AuditAksiTests(_Base):
    def test_reconcile_tercatat(self):
        # Auto-split: butuh data panel + uang sehari + checkbox sumber tercentang.
        up = Upload.objects.create(source_type=self.panel, toko=self.lbs)
        Transaction.objects.create(
            upload=up, source_type=self.panel, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 28, 10, 0), row_hash="rc1", username="budi",
        )
        Transaction.objects.create(
            upload=up, source_type=self.bank, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 28, 11, 0), row_hash="rc2", username="budi",
        )
        self.client.post(reverse("reconcile"), {
            "tolerance": "Default", "inc_panel_dp": "on", "inc_bank": "on",
        })
        log = AuditLog.objects.filter(aksi="reconcile").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.user, self.adm)
        self.assertEqual(log.toko, self.lbs)
        self.assertIn("batch_pk", log.detail)

    def test_hapus_batch_tercatat(self):
        batch, _run, _r = self._batch_dengan_result()
        self.client.post(reverse("delete_batch", args=[batch.pk]))
        log = AuditLog.objects.filter(aksi="hapus_batch").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.user, self.adm)
        self.assertEqual(log.detail.get("batch_pk"), batch.pk)

    def test_hapus_upload_tercatat(self):
        up = Upload.objects.create(
            source_type=self.panel, toko=self.lbs, original_name="panel.xlsx"
        )
        self.client.post(reverse("delete_upload", args=[up.pk]))
        log = AuditLog.objects.filter(aksi="hapus_upload").first()
        self.assertIsNotNone(log)
        self.assertIn("panel.xlsx", log.objek)

    def test_review_tercatat(self):
        _batch, _run, r = self._batch_dengan_result()
        self.client.post(reverse("review", args=[r.pk]), {"action": "mark_matched"})
        log = AuditLog.objects.filter(aksi="review").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.detail.get("result_pk"), r.pk)

    def test_review_bulk_tercatat_satu_entri(self):
        _batch, run, r = self._batch_dengan_result()
        self.client.post(reverse("bulk_review", args=[run.pk]), {
            "action": "mark_matched", "result_ids": [r.pk],
        })
        log = AuditLog.objects.filter(aksi="review_massal").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.detail.get("n"), 1)


class RiwayatAksiTampilTests(_Base):
    def test_batch_detail_menampilkan_riwayat(self):
        batch, _run, _r = self._batch_dengan_result()
        AuditLog.objects.create(
            user=self.adm, toko=self.lbs, aksi="reconcile",
            objek=f"Batch #{batch.pk}", detail={"batch_pk": batch.pk},
        )
        resp = self.client.get(reverse("batch_detail", args=[batch.pk]))
        self.assertContains(resp, "Riwayat aksi")
        self.assertContains(resp, "adm")

    def test_riwayat_terfilter_toko_batch(self):
        """W3-7: filter riwayat menyertakan toko=batch.toko — index (toko, aksi)
        AuditLog terpakai; batch_pk toko lain (tabrakan pk) tak ikut tampil."""
        batch, _run, _r = self._batch_dengan_result()
        AuditLog.objects.create(
            user=self.adm, toko=self.lbs, aksi="reconcile",
            objek="Batch asli", detail={"batch_pk": batch.pk},
        )
        toko_lain = Toko.objects.exclude(pk=self.lbs.pk).first()
        AuditLog.objects.create(
            user=self.adm, toko=toko_lain, aksi="reconcile",
            objek="BATCH-TOKO-LAIN", detail={"batch_pk": batch.pk},
        )
        resp = self.client.get(reverse("batch_detail", args=[batch.pk]))
        self.assertContains(resp, "Batch asli")
        self.assertNotContains(resp, "BATCH-TOKO-LAIN")
