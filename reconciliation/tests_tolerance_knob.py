"""W5-7: knob ToleranceProfile harus jujur & tersambung ke engine.

amount_abs_tol dulu TIDAK berpengaruh di matcher uang — band near-miss pass 2
hardcode max(2500, amt//100). Profil dgn abs_tol besar kini benar-benar
melebarkan band; Default (abs_tol=0) tidak berubah perilaku.
"""
from datetime import datetime
from decimal import Decimal

from django.test import TestCase

from reconciliation.engine import amount_ok, run_match
from reconciliation.models import MatchResult, MatchRun, ToleranceProfile
from reconciliation.tests_matcher_v2 import _Base

D = Decimal


class AbsTolBandTests(_Base):
    """Profil amount_abs_tol besar melebarkan band near-miss pass 2."""

    def _run(self, tol):
        return run_match(MatchRun.Relation.PANEL_BANK, tol, toko=self.toko)

    def test_abs_tol_10000_selisih_8000_masuk_band(self):
        # Band lama: max(2500, 100000//100) = 2500 → selisih 8000 lolos dari
        # near-miss dan berakhir no_money. Dengan abs_tol=10000 harus terpasang.
        tol = ToleranceProfile.objects.create(
            name="AbsTol10k (tes)", date_window_days=1, amount_abs_tol=D("10000"),
        )
        p = self.tx(self.panel, self.up_panel, "depo", "100000", "100000",
                    datetime(2026, 6, 27, 10), user="budi88", cp="BUDI SANTOSO")
        b = self.tx(self.bank, self.up_hendi, "depo", "92000", "92000",
                    datetime(2026, 6, 27, 11), user="budi88", cp="BUDI SANTOSO")
        self._run(tol)
        r = MatchResult.objects.get(left=p)
        self.assertEqual(r.right_id, b.id)
        self.assertEqual(r.reason_code, "amount_fee")
        self.assertEqual(r.bucket, MatchResult.Bucket.COCOK)  # identitas persis

    def test_default_abs_tol_0_perilaku_lama(self):
        # Default (abs_tol=0): selisih 8000 tetap di luar band → no_money.
        p = self.tx(self.panel, self.up_panel, "depo", "100000", "100000",
                    datetime(2026, 6, 27, 10), user="budi88", cp="BUDI SANTOSO")
        self.tx(self.bank, self.up_hendi, "depo", "92000", "92000",
                datetime(2026, 6, 27, 11), user="budi88", cp="BUDI SANTOSO")
        self._run(self.tol)
        r = MatchResult.objects.get(left=p)
        self.assertIsNone(r.right_id)
        self.assertEqual(r.reason_code, "no_money")


class AmountOkTailTests(TestCase):
    """Baris akhir amount_ok: di luar semua toleransi = False, titik.
    (Bentuk lama `return diff == 0, diff` tak pernah True — diff 0 sudah
    tertangkap cabang abs_tol.)"""

    def test_di_luar_toleransi_false(self):
        tol = ToleranceProfile(amount_abs_tol=D("0"), amount_pct_tol=D("0"))
        ok, diff = amount_ok(D("100000"), D("92000"), tol)
        self.assertFalse(ok)
        self.assertEqual(diff, D("8000"))

    def test_dalam_abs_tol_true(self):
        tol = ToleranceProfile(amount_abs_tol=D("10000"), amount_pct_tol=D("0"))
        ok, diff = amount_ok(D("100000"), D("92000"), tol)
        self.assertTrue(ok)
        self.assertEqual(diff, D("8000"))
