"""W1-1: run_batch mengunci baris Toko (select_for_update) — serialisasi per toko.

Dua run konkuren (meski beda tanggal) membaca pool aktif yang sama → bisa
menghasilkan MatchResult ganda / summary dobel. Lock baris Toko menahan run
kedua sampai run pertama commit.

Perilaku konkuren tidak bisa diuji di sqlite (select_for_update = no-op di
backend tanpa dukungan FOR UPDATE), jadi di sini:
1) spy QuerySet.select_for_update memastikan lock DIMINTA untuk model Toko;
2) smoke test memastikan run_batch tetap berjalan normal dengan lock terpasang.
"""
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from django.db.models import QuerySet
from django.test import TestCase

from reconciliation.engine import run_batch
from reconciliation.models import ToleranceProfile
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction


class RunBatchLockTests(TestCase):
    def setUp(self):
        self.lbs = Toko.objects.get(key="lbs")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.up = Upload.objects.create(source_type=self.panel, toko=self.lbs)

    def _tx(self, st, jenis, amount, money, ticket, rh, dt=datetime(2026, 6, 27, 10, 0), **kw):
        return Transaction.objects.create(
            upload=self.up, source_type=st, toko=self.lbs, jenis=jenis,
            amount=Decimal(amount), money_delta=Decimal(money), ticket_no=ticket,
            occurred_at=dt, row_hash=rh, **kw,
        )

    def test_run_batch_meminta_lock_baris_toko(self):
        self._tx(self.panel, "depo", "50000", "50000", "D1", "p1", username="budi")
        self._tx(self.bank, "depo", "50000", "50000", "", "k1", username="budi")

        locked_models = []
        orig = QuerySet.select_for_update

        def spy(qs, *args, **kwargs):
            locked_models.append(qs.model.__name__)
            return orig(qs, *args, **kwargs)

        with mock.patch.object(QuerySet, "select_for_update", spy):
            batch = run_batch(self.lbs, self.tol, recon_date=date(2026, 6, 27))

        self.assertIn("Toko", locked_models)  # lock per-toko diminta di awal run
        # Smoke: run tetap normal — pasangan cocok & konsumsi jalan.
        self.assertEqual(batch.summary["buckets"]["cocok"], 1)
        self.assertEqual(
            Transaction.objects.filter(consumed_by_batch=batch).count(), 2
        )
