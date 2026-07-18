from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from reconciliation.engine import run_batch
from reconciliation.models import (
    MatchResult,
    MatchRun,
    ReconBatch,
    ReviewAction,
    ToleranceProfile,
)
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction


class ReconcileViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        ToleranceProfile.objects.get_or_create(name="Default", defaults={"date_window_days": 1})
        self.lbs = Toko.objects.get(key="lbs")
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})
        panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        up = Upload.objects.create(source_type=panel, toko=self.lbs)
        for st, rh in [(panel, "r1"), (bank, "r2")]:
            Transaction.objects.create(
                upload=up, source_type=st, toko=self.lbs, jenis="depo",
                amount=Decimal("50000"), money_delta=Decimal("50000"),
                occurred_at=datetime(2026, 6, 27, 10, 0), row_hash=rh,
            )

    def test_get_shows_completeness(self):
        r = self.client.get(reverse("reconcile"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["completeness"]["minimum_met"])

    def test_post_runs_batch_and_redirects(self):
        # Auto-split: tanggal dideteksi dari data (27 Jun), tanpa recon_date manual.
        # inc_* meniru checkbox form yang tercentang default untuk sumber siap.
        r = self.client.post(reverse("reconcile"),
                             {"tolerance": "Default", "inc_panel_dp": "on", "inc_bank": "on"})
        self.assertEqual(r.status_code, 302)
        batch = ReconBatch.objects.latest("id")
        self.assertEqual(r.url, reverse("batch_detail", args=[batch.pk]))
        self.assertEqual(batch.toko, self.lbs)
        self.assertEqual(batch.recon_date.isoformat(), "2026-06-27")

    def test_post_unknown_tolerance_returns_404(self):
        n = ReconBatch.objects.count()
        r = self.client.post(reverse("reconcile"), {"tolerance": "nope"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(ReconBatch.objects.count(), n)  # tak ada batch dibuat

    def test_post_blokir_uang_yatim_tak_bikin_batch(self):
        # Panel 27 (setUp) & 30 = rentang; bank 29 DALAM rentang tanpa panel penutup
        # (window 1) → run ditolak, 0 batch.
        panel = SourceType.objects.get(key="panel")
        bank = SourceType.objects.get(key="bank")
        up = Upload.objects.create(source_type=panel, toko=self.lbs)
        for st, rh in [(panel, "p30"), (bank, "b30")]:
            Transaction.objects.create(
                upload=up, source_type=st, toko=self.lbs, jenis="depo",
                amount=Decimal("80000"), money_delta=Decimal("80000"),
                occurred_at=datetime(2026, 6, 30, 10, 0), row_hash=rh,
            )
        Transaction.objects.create(
            upload=up, source_type=bank, toko=self.lbs, jenis="depo",
            amount=Decimal("90000"), money_delta=Decimal("90000"),
            occurred_at=datetime(2026, 6, 29, 10, 0), row_hash="b29",
        )
        n = ReconBatch.objects.count()
        r = self.client.post(
            reverse("reconcile"),
            {"tolerance": "Default", "inc_panel_dp": "on", "inc_bank": "on"},
            follow=True,
        )
        self.assertEqual(ReconBatch.objects.count(), n)
        self.assertContains(r, "ditolak")
        self.assertContains(r, "29/06/2026")

    def test_get_menampilkan_preview_tanggal(self):
        r = self.client.get(reverse("reconcile"))
        self.assertEqual(r.context["panel_dates_count"], 1)
        self.assertContains(r, "tanggal panel terdeteksi")


class BatchDetailTests(TestCase):
    def setUp(self):
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        self.tol = ToleranceProfile.objects.get_or_create(name="Default", defaults={"date_window_days": 1})[0]
        self.lbs = Toko.objects.get(key="lbs")
        panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        up = Upload.objects.create(source_type=panel, toko=self.lbs)
        for st, rh in [(panel, "d1"), (bank, "d2")]:
            Transaction.objects.create(
                upload=up, source_type=st, toko=self.lbs, jenis="depo",
                amount=Decimal("50000"), money_delta=Decimal("50000"),
                occurred_at=datetime(2026, 6, 27, 10, 0), row_hash=rh,
            )

    def test_batch_detail_renders(self):
        batch = run_batch(self.lbs, self.tol)
        r = self.client.get(reverse("batch_detail", args=[batch.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Deposit")
        self.assertContains(r, "Withdraw")


class ReviewViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.lbs = Toko.objects.get(key="lbs")
        batch = ReconBatch.objects.create(toko=self.lbs, tolerance=self.tol)
        run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=batch
        )
        # Hasil BERPASANGAN (guard W1-6a: mark_matched butuh baris uang).
        bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        up_b = Upload.objects.create(source_type=bank, toko=self.lbs)
        right = Transaction.objects.create(
            upload=up_b, source_type=bank, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 27, 11, 0), row_hash="rv-b1",
        )
        self.result = MatchResult.objects.create(
            run=run, bucket=MatchResult.Bucket.TINJAU, reason_code="init", right=right
        )
        self.url = reverse("review", args=[self.result.pk])

    def test_get_returns_405_and_no_side_effects(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 405)
        self.assertEqual(ReviewAction.objects.count(), 0)
        self.result.refresh_from_db()
        self.assertEqual(self.result.bucket, MatchResult.Bucket.TINJAU)
        self.assertEqual(self.result.reason_code, "init")

    def test_post_invalid_action_returns_400_and_no_side_effects(self):
        r = self.client.post(self.url, {"action": "explode"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ReviewAction.objects.count(), 0)
        self.result.refresh_from_db()
        self.assertEqual(self.result.bucket, MatchResult.Bucket.TINJAU)
        self.assertEqual(self.result.reason_code, "init")

    def test_post_missing_action_returns_400_and_no_side_effects(self):
        r = self.client.post(self.url, {})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(ReviewAction.objects.count(), 0)
        self.result.refresh_from_db()
        self.assertEqual(self.result.bucket, MatchResult.Bucket.TINJAU)

    def test_post_valid_action_updates_and_logs(self):
        r = self.client.post(self.url, {"action": "mark_matched"})
        self.assertEqual(r.status_code, 200)
        self.result.refresh_from_db()
        self.assertEqual(self.result.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(self.result.reason_code, "manual_override")
        self.assertEqual(ReviewAction.objects.count(), 1)
        ra = ReviewAction.objects.get()
        self.assertEqual(ra.action, "mark_matched")
