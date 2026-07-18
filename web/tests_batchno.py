"""W4-2 — batch_no helper: nilai identik rumus lama, versi peta = 1 query."""
from django.test import TestCase

from reconciliation.models import ReconBatch, ToleranceProfile
from sources.models import Toko
from web.batchno import batch_no, batch_no_map, batch_no_map_multi


class BatchNoTests(TestCase):
    def setUp(self):
        self.lbs = Toko.objects.get(key="lbs")
        self.lain = Toko.objects.exclude(pk=self.lbs.pk).first()
        self.tol = ToleranceProfile.objects.get(name="Default")
        # selang-seling antar toko supaya id global ≠ posisi per-toko
        self.b1 = ReconBatch.objects.create(toko=self.lbs, tolerance=self.tol)
        self.x1 = ReconBatch.objects.create(toko=self.lain, tolerance=self.tol)
        self.b2 = ReconBatch.objects.create(toko=self.lbs, tolerance=self.tol)
        self.b3 = ReconBatch.objects.create(toko=self.lbs, tolerance=self.tol)

    def _lama(self, b):
        return ReconBatch.objects.filter(toko=b.toko, id__lte=b.id).count()

    def test_nilai_sama_dengan_rumus_lama(self):
        for b in (self.b1, self.b2, self.b3, self.x1):
            self.assertEqual(batch_no(b), self._lama(b))
        peta = batch_no_map(self.lbs, [self.b1.id, self.b2.id, self.b3.id])
        self.assertEqual(peta[self.b1.id], 1)
        self.assertEqual(peta[self.b2.id], 2)
        self.assertEqual(peta[self.b3.id], 3)

    def test_peta_satu_query(self):
        ids = [self.b1.id, self.b2.id, self.b3.id]
        with self.assertNumQueries(1):
            batch_no_map(self.lbs, ids)

    def test_peta_kosong_tanpa_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(batch_no_map(self.lbs, []), {})
        with self.assertNumQueries(0):
            self.assertEqual(batch_no_map(self.lbs, [None]), {})

    def test_multi_toko(self):
        peta = batch_no_map_multi([
            (self.b2.id, self.lbs.id), (self.x1.id, self.lain.id),
        ])
        self.assertEqual(peta[self.b2.id], 2)
        self.assertEqual(peta[self.x1.id], 1)

    def test_batch_none(self):
        self.assertIsNone(batch_no(None))
