"""W4-6/W4-2 — anggaran query halaman hasil: jumlah query TIDAK ikut jumlah
baris (select_related lengkap utk source_label_full + peta nomor batch).
Asersi berbentuk 'konstan' (render 2 baris = render 8 baris), bukan angka
pasti — kebal penambahan query kecil yang tak per-baris."""
from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from reconciliation.models import MatchResult, MatchRun, ReconBatch, ToleranceProfile
from sources.models import Account, SourceType, Toko, Upload
from transactions.models import Transaction

User = get_user_model()
_seq = iter(range(1, 100000))


class _Base(TestCase):
    def setUp(self):
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        self.toko = Toko.objects.get(key="lbs")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.acc = Account.objects.create(
            kind=Account.BANK, provider="BCA", name="BCA HENDI", account_no="123"
        )
        self.up_p = Upload.objects.create(source_type=self.panel, toko=self.toko)
        self.up_b = Upload.objects.create(
            source_type=self.bank, toko=self.toko, account=self.acc,
            original_name="BCA HENDI.xlsx", owner_name="HENDI",
        )
        self.batch = ReconBatch.objects.create(toko=self.toko, tolerance=self.tol)
        self.run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=self.batch
        )
        self.client.post(reverse("set_toko"), {"toko_id": self.toko.id})

    def _pair(self):
        n = next(_seq)
        left = Transaction.objects.create(
            upload=self.up_p, source_type=self.panel, toko=self.toko, jenis="depo",
            amount=Decimal("50000"), occurred_at=datetime(2026, 6, 27, 10, 0),
            ticket_no=f"D{n}", row_hash=f"qp-{n}", raw={},
        )
        right = Transaction.objects.create(
            upload=self.up_b, source_type=self.bank, toko=self.toko, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 27, 11, 0), counterparty="BUDI",
            row_hash=f"qb-{n}", raw={},
        )
        MatchResult.objects.create(
            run=self.run, bucket=MatchResult.Bucket.TINJAU,
            reason_code="name_partial", left=left, right=right, score=70,
        )

    def _n_queries(self, url):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return len(ctx.captured_queries)

    def _assert_konstan(self, url):
        for _ in range(2):
            self._pair()
        self.client.get(url)  # warm-up: query sesi/first-request tak ikut diukur
        n_kecil = self._n_queries(url)
        for _ in range(6):
            self._pair()
        n_besar = self._n_queries(url)
        self.assertEqual(
            n_kecil, n_besar,
            f"jumlah query ikut jumlah baris: {n_kecil} → {n_besar}",
        )


class RunDetailQueryBudgetTests(_Base):
    def test_query_konstan_terhadap_jumlah_baris(self):
        self._assert_konstan(reverse("run_detail", args=[self.run.pk]))


class ReviewQueueQueryBudgetTests(_Base):
    def test_query_konstan_terhadap_jumlah_baris(self):
        self._assert_konstan(reverse("review_queue"))
