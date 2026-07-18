"""Override manual (review/bulk_review) harus MENYEGARKAN summary run & batch —
kartu "Cocok/Perlu Ditinjau" tidak boleh basi terhadap chip yang dihitung live."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from reconciliation.models import MatchResult, MatchRun, ReconBatch, ToleranceProfile
from sources.models import Toko


class ReviewRefreshSummaryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.lbs = Toko.objects.get(key="lbs")
        self.batch = ReconBatch.objects.create(
            toko=self.lbs, tolerance=self.tol,
            summary={"buckets": {"cocok": 0, "perlu_tinjau": 2, "tidak_cocok": 0},
                     "dp": {"panel": 0.0}, "wd": {"panel": 0.0}},
        )
        self.run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=self.batch,
            summary={"cocok": 0, "perlu_tinjau": 2, "tidak_cocok": 0},
        )
        # Hasil BERPASANGAN (guard W1-6a: mark_matched butuh baris uang).
        from datetime import datetime
        from decimal import Decimal

        from sources.models import SourceType, Upload
        from transactions.models import Transaction

        bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        up_b = Upload.objects.create(source_type=bank, toko=self.lbs)

        def _uang(rh, jam):
            return Transaction.objects.create(
                upload=up_b, source_type=bank, toko=self.lbs, jenis="depo",
                amount=Decimal("50000"), money_delta=Decimal("50000"),
                occurred_at=datetime(2026, 6, 27, jam, 0), row_hash=rh,
            )

        self.r1 = MatchResult.objects.create(
            run=self.run, bucket=MatchResult.Bucket.TINJAU, reason_code="weak_name",
            right=_uang("rr-b1", 10),
        )
        self.r2 = MatchResult.objects.create(
            run=self.run, bucket=MatchResult.Bucket.TINJAU, reason_code="weak_name",
            right=_uang("rr-b2", 11),
        )

    def test_review_tunggal_menyegarkan_summary_run_dan_batch(self):
        r = self.client.post(
            reverse("review", args=[self.r1.pk]), {"action": "mark_matched"}
        )
        self.assertEqual(r.status_code, 200)
        self.run.refresh_from_db()
        self.assertEqual(self.run.summary["cocok"], 1)
        self.assertEqual(self.run.summary["perlu_tinjau"], 1)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.summary["buckets"]["cocok"], 1)
        self.assertEqual(self.batch.summary["buckets"]["perlu_tinjau"], 1)

    def test_bulk_review_menyegarkan_summary_run_dan_batch(self):
        r = self.client.post(
            reverse("bulk_review", args=[self.run.pk]),
            {"action": "mark_matched", "result_ids": [self.r1.pk, self.r2.pk]},
        )
        self.assertEqual(r.status_code, 302)
        self.run.refresh_from_db()
        self.assertEqual(self.run.summary["cocok"], 2)
        self.assertEqual(self.run.summary["perlu_tinjau"], 0)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.summary["buckets"]["cocok"], 2)
        self.assertEqual(self.batch.summary["buckets"]["perlu_tinjau"], 0)


class MarkUnmatchedMoneyTests(TestCase):
    """mark_unmatched pada hasil BERPASANGAN harus MENGURANGI money_matched —
    uang yang pasangannya ditolak auditor tidak boleh tetap dihitung matched."""

    def setUp(self):
        from datetime import datetime
        from decimal import Decimal

        from django.contrib.auth import get_user_model

        from sources.models import SourceType, Upload
        from transactions.models import Transaction

        User = get_user_model()
        User.objects.create_user("aud2", "b@b.co", "pw12345", role="supervisor")
        self.client.login(username="aud2", password="pw12345")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.lbs = Toko.objects.get(key="lbs")
        panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        up = Upload.objects.create(source_type=panel, toko=self.lbs)
        self.left = Transaction.objects.create(
            upload=up, source_type=panel, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 27, 10, 0), row_hash="mu-p",
        )
        self.right = Transaction.objects.create(
            upload=up, source_type=bank, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 27, 11, 0), row_hash="mu-b",
        )
        self.batch = ReconBatch.objects.create(
            toko=self.lbs, tolerance=self.tol,
            summary={"buckets": {"cocok": 1, "perlu_tinjau": 0, "tidak_cocok": 0},
                     "dp": {"panel": 50000.0, "money_matched": 50000.0, "money": 50000.0,
                            "money_gross": 50000.0, "selisih": 0.0},
                     "wd": {"panel": 0.0}},
        )
        self.run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=self.batch,
            summary={"cocok": 1, "perlu_tinjau": 0, "tidak_cocok": 0},
        )
        self.res = MatchResult.objects.create(
            run=self.run, bucket=MatchResult.Bucket.COCOK, left=self.left,
            right=self.right, score=100, reason_code="amount+date+name",
        )

    def test_mark_unmatched_mengeluarkan_uang_dari_matched(self):
        from django.urls import reverse as rv

        r = self.client.post(rv("review", args=[self.res.pk]), {"action": "mark_unmatched"})
        self.assertEqual(r.status_code, 200)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.summary["dp"]["money_matched"], 0.0)
        self.assertEqual(self.batch.summary["dp"]["selisih"], 50000.0)
