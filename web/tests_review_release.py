"""Review menolak pasangan → sisi uang harus DIBEBASKAN kembali ke pool aktif.

Pasangan weak_name (perlu_tinjau) mengonsumsi baris uang saat run_batch
(used + spillover) SEBELUM auditor memutuskan. Kalau auditor menolak lewat
review/review_bulk (mark_unmatched), baris uang tadinya tetap
consumed_by_batch=batch lama — tak pernah kembali ke pool aktif, sehingga
batch lain / re-match tak bisa memakainya.

Kontrak yang diuji:
- mark_unmatched pada hasil berpasangan → bucket tidak_cocok, right DILEPAS,
  dan Transaction.consumed_by_batch dibebaskan BILA yang mengonsumsi = batch
  pemilik run dan baris tak dipakai MatchResult lain.
- Konsumsi milik batch LAIN atau baris yang masih dipakai bukti lain TIDAK
  disentuh.
- Aksi selain mark_unmatched tidak melepas apa pun.
- Pembebasan tercatat di audit trail (core.audit.catat).
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
        self.batch = ReconBatch.objects.create(
            toko=self.lbs, tolerance=self.tol,
            date_from=date(2026, 6, 27), date_to=date(2026, 6, 27),
        )
        self.run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=self.batch,
            summary={"cocok": 0, "perlu_tinjau": 1, "tidak_cocok": 0},
        )
        self.up_panel = Upload.objects.create(source_type=self.panel, toko=self.lbs)
        self.up_bank = Upload.objects.create(source_type=self.bank, toko=self.lbs)

    def _pasangan_weak_name(self, i=0, consumed_by=...):
        """Satu pasangan weak_name: panel depo + uang bank yang SUDAH dikonsumsi."""
        if consumed_by is ...:
            consumed_by = self.batch
        p = Transaction.objects.create(
            upload=self.up_panel, source_type=self.panel, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 27, 21, 0), row_hash=f"pl{i}",
        )
        b = Transaction.objects.create(
            upload=self.up_bank, source_type=self.bank, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 27, 21, 5), row_hash=f"bk{i}",
            consumed_by_batch=consumed_by,
        )
        r = MatchResult.objects.create(
            run=self.run, bucket=MatchResult.Bucket.TINJAU, left=p, right=b,
            score=55, reason_code="weak_name",
            reason_detail="nominal+tanggal cocok, nama lemah (score 55)",
        )
        return p, b, r


class ReviewRejectReleaseTests(_Base):
    def test_reject_weak_name_bebaskan_uang(self):
        p, b, r = self._pasangan_weak_name()
        resp = self.client.post(reverse("review", args=[r.pk]), {"action": "mark_unmatched"})
        self.assertEqual(resp.status_code, 200)
        r.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)
        self.assertIsNone(r.right_id)  # pasangan dilepas
        self.assertIsNone(b.consumed_by_batch_id)  # uang kembali ke pool aktif

    def test_reject_tercatat_di_audit(self):
        p, b, r = self._pasangan_weak_name()
        self.client.post(reverse("review", args=[r.pk]), {"action": "mark_unmatched"})
        log = AuditLog.objects.filter(aksi="review").last()
        self.assertIsNotNone(log)
        self.assertEqual(log.detail.get("uang_dibebaskan"), b.pk)

    def test_reject_tidak_sentuh_konsumsi_batch_lain(self):
        batch_lain = ReconBatch.objects.create(
            toko=self.lbs, tolerance=self.tol,
            date_from=date(2026, 6, 26), date_to=date(2026, 6, 26),
        )
        p, b, r = self._pasangan_weak_name(consumed_by=batch_lain)
        self.client.post(reverse("review", args=[r.pk]), {"action": "mark_unmatched"})
        r.refresh_from_db()
        b.refresh_from_db()
        self.assertIsNone(r.right_id)  # pasangan tetap dilepas
        self.assertEqual(b.consumed_by_batch_id, batch_lain.pk)  # konsumsi batch lain utuh

    def test_reject_tidak_bebaskan_bila_dipakai_hasil_lain(self):
        p, b, r = self._pasangan_weak_name()
        run2 = MatchRun.objects.create(
            relation=MatchRun.Relation.BRACKET_BANK, tolerance=self.tol, batch=self.batch,
        )
        lain = MatchResult.objects.create(
            run=run2, bucket=MatchResult.Bucket.COCOK, right=b, score=100,
        )
        self.client.post(reverse("review", args=[r.pk]), {"action": "mark_unmatched"})
        r.refresh_from_db()
        b.refresh_from_db()
        lain.refresh_from_db()
        self.assertIsNone(r.right_id)
        self.assertEqual(b.consumed_by_batch_id, self.batch.pk)  # bukti lain masih mengunci
        self.assertEqual(lain.right_id, b.pk)  # hasil lain tak disentuh

    def test_mark_matched_tidak_lepas_apapun(self):
        p, b, r = self._pasangan_weak_name()
        self.client.post(reverse("review", args=[r.pk]), {"action": "mark_matched"})
        r.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(r.right_id, b.pk)
        self.assertEqual(b.consumed_by_batch_id, self.batch.pk)


class ReviewBulkRejectReleaseTests(_Base):
    def test_bulk_reject_bebaskan_semua_uang(self):
        p1, b1, r1 = self._pasangan_weak_name(0)
        p2, b2, r2 = self._pasangan_weak_name(1)
        resp = self.client.post(reverse("review_bulk"), {
            "result_ids": [r1.pk, r2.pk], "action": "mark_unmatched",
        })
        self.assertEqual(resp.status_code, 302)
        for r, b in ((r1, b1), (r2, b2)):
            r.refresh_from_db()
            b.refresh_from_db()
            self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)
            self.assertIsNone(r.right_id)
            self.assertIsNone(b.consumed_by_batch_id)
        log = AuditLog.objects.filter(aksi="review_massal").last()
        self.assertEqual(log.detail.get("uang_dibebaskan"), 2)

    def test_bulk_reject_right_kembar_tetap_bebas(self):
        # Re-match bisa memasangkan SATU uang ke beberapa target duplikat —
        # tolak keduanya sekaligus harus tetap membebaskan uangnya.
        p1, b1, r1 = self._pasangan_weak_name(0)
        p2 = Transaction.objects.create(
            upload=self.up_panel, source_type=self.panel, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 27, 21, 1), row_hash="pl-dup",
        )
        r2 = MatchResult.objects.create(
            run=self.run, bucket=MatchResult.Bucket.TINJAU, left=p2, right=b1,
            score=55, reason_code="weak_name",
        )
        self.client.post(reverse("review_bulk"), {
            "result_ids": [r1.pk, r2.pk], "action": "mark_unmatched",
        })
        b1.refresh_from_db()
        self.assertIsNone(b1.consumed_by_batch_id)

    def test_bulk_mark_review_tidak_lepas(self):
        p, b, r = self._pasangan_weak_name()
        self.client.post(reverse("review_bulk"), {
            "result_ids": [r.pk], "action": "mark_review",
        })
        r.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(r.right_id, b.pk)
        self.assertEqual(b.consumed_by_batch_id, self.batch.pk)
