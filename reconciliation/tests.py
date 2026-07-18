from datetime import datetime
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from reconciliation.engine import _name_score, amount_ok, date_ok, run_match
from reconciliation.models import ToleranceProfile
from sources.models import SourceType, Upload
from transactions.models import Transaction


class MatchResultIndexTests(SimpleTestCase):
    """W3-7: MatchResult difilter per bucket/reason_code hampir tiap request
    (context processor antrean tinjau, _carried_qs, review_queue) — wajib
    ter-index, bukan seq-scan tabel hasil terbesar."""

    def test_meta_indexes_bucket_dan_reason_code(self):
        from reconciliation.models import MatchResult

        idx = [tuple(i.fields) for i in MatchResult._meta.indexes]
        self.assertIn(("bucket",), idx)
        self.assertIn(("reason_code", "bucket"), idx)


class NameScoreTests(SimpleTestCase):
    """Skor nama toleran-truncation: bank sering memotong nama (BCA ~18 karakter)."""

    def test_nama_terpotong_bca_18_char_tetap_lolos_threshold(self):
        # Kasus wajib: sisi BCA "M. YULIANSAR SIREG" vs Panel "M. YULIANSAR SIREGAR"
        self.assertGreaterEqual(_name_score("M. YULIANSAR SIREGAR", "M. YULIANSAR SIREG"), 85)

    def test_nama_terpotong_bri_tetap_lolos_threshold(self):
        self.assertGreaterEqual(_name_score("MARIO KARO-KARO", "MARIO KARO-KAR"), 85)

    def test_nama_identik_skor_100(self):
        self.assertEqual(_name_score("BUDI SANTOSO", "BUDI SANTOSO"), 100)

    def test_nama_beda_tetap_rendah(self):
        self.assertLess(_name_score("BUDI SANTOSO", "SITI AMINAH"), 85)

    def test_nama_pendek_tidak_dapat_bonus_prefix(self):
        # Nama sangat pendek jangan dianggap "terpotong" dari nama lain (false positive)
        self.assertLess(_name_score("ITA", "ROSITA WATI NINGSIH"), 85)

    def test_kosong_skor_nol(self):
        self.assertEqual(_name_score("", ""), 0)
        self.assertEqual(_name_score("BUDI", ""), 0)


class ToleranceHelperTests(TestCase):
    def setUp(self):
        self.tol = ToleranceProfile(
            date_window_days=1,
            amount_abs_tol=Decimal("0"),
            amount_pct_tol=Decimal("0"),
            fuzzy_threshold=85,
        )

    def test_amount_exact(self):
        ok, _ = amount_ok(Decimal("50000"), Decimal("50000"), self.tol)
        self.assertTrue(ok)

    def test_amount_diff(self):
        ok, diff = amount_ok(Decimal("100000"), Decimal("80000"), self.tol)
        self.assertFalse(ok)
        self.assertEqual(diff, Decimal("20000"))

    def test_amount_within_abs_tolerance(self):
        self.tol.amount_abs_tol = Decimal("700")  # biaya admin 627
        ok, _ = amount_ok(Decimal("57000"), Decimal("56373"), self.tol)
        self.assertTrue(ok)

    def test_date_t1_ok(self):
        self.assertTrue(date_ok(datetime(2026, 6, 27, 23, 59), datetime(2026, 6, 28, 6, 0), self.tol))

    def test_date_same_day_ok(self):
        self.assertTrue(date_ok(datetime(2026, 6, 27, 10, 0), datetime(2026, 6, 27, 12, 0), self.tol))

    def test_date_bank_earlier_fails(self):
        self.assertFalse(date_ok(datetime(2026, 6, 28, 0, 0), datetime(2026, 6, 27, 0, 0), self.tol))

    def test_date_too_far_fails(self):
        self.assertFalse(date_ok(datetime(2026, 6, 27, 0, 0), datetime(2026, 6, 29, 0, 0), self.tol))


class PanelBracketMatcherTests(TestCase):
    def setUp(self):
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bracket = SourceType.objects.get_or_create(key="bracket", defaults={"name": "Bracket"})[0]
        self.tol = ToleranceProfile.objects.get_or_create(name="Default", defaults={"date_window_days": 1})[0]
        self.up_p = Upload.objects.create(source_type=self.panel)
        self.up_b = Upload.objects.create(source_type=self.bracket)

    def _tx(self, st, up, ticket, amount, **kw):
        return Transaction.objects.create(
            upload=up, source_type=st, row_hash=f"{ticket}-{amount}-{st.key}",
            ticket_no=ticket, amount=Decimal(amount), jenis="depo", **kw,
        )

    def test_ticket_and_amount_match(self):
        self._tx(self.panel, self.up_p, "D1", "20000", occurred_at=datetime(2026, 6, 27, 23, 59))
        self._tx(self.bracket, self.up_b, "D1", "20000", occurred_at=datetime(2026, 6, 28, 0, 0))
        run = run_match("panel_bracket", self.tol)
        self.assertEqual(run.summary["cocok"], 1)
        self.assertEqual(run.summary["tidak_cocok"], 0)

    def test_amount_mismatch_flagged_for_review(self):
        self._tx(self.panel, self.up_p, "D2", "100000")
        self._tx(self.bracket, self.up_b, "D2", "80000")
        run = run_match("panel_bracket", self.tol)
        self.assertEqual(run.summary["perlu_tinjau"], 1)
        self.assertEqual(run.summary["cocok"], 0)

    def test_unmatched_both_sides(self):
        self._tx(self.panel, self.up_p, "D3", "50000")
        self._tx(self.bracket, self.up_b, "D9", "50000")
        run = run_match("panel_bracket", self.tol)
        self.assertEqual(run.summary["cocok"], 0)
        self.assertEqual(run.summary["tidak_cocok"], 2)


class PanelBankMatcherTests(TestCase):
    def setUp(self):
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.gw = SourceType.objects.get_or_create(key="gateway", defaults={"name": "Gateway", "is_money_source": True})[0]
        self.tol = ToleranceProfile.objects.get_or_create(name="Default", defaults={"date_window_days": 1, "fuzzy_threshold": 85})[0]
        self.up = Upload.objects.create(source_type=self.panel)
        self.upg = Upload.objects.create(source_type=self.gw)

    def test_username_amount_date_match(self):
        Transaction.objects.create(
            upload=self.up, source_type=self.panel, row_hash="p1", jenis="depo",
            amount=Decimal("57000"), money_delta=Decimal("57000"),
            username="andysudrajat", occurred_at=datetime(2026, 6, 27, 0, 0),
        )
        Transaction.objects.create(
            upload=self.upg, source_type=self.gw, row_hash="g1", jenis="depo",
            amount=Decimal("57000"), money_delta=Decimal("57000"),
            username="andysudrajat", occurred_at=datetime(2026, 6, 27, 0, 2),
        )
        run = run_match("panel_bank", self.tol)
        self.assertEqual(run.summary["cocok"], 1)


class RunMatchAtomicTests(TestCase):
    """W5-8c: exception di tengah matcher tidak boleh meninggalkan MatchRun
    yatim (run tanpa hasil yang mengotori riwayat & summary)."""

    def test_matcher_raise_tidak_ada_run_tersisa(self):
        from unittest.mock import patch

        from reconciliation.engine import PanelBankMatcher
        from reconciliation.models import MatchRun

        tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1, "fuzzy_threshold": 85}
        )[0]
        with patch.object(PanelBankMatcher, "match", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                run_match("panel_bank", tol)
        self.assertEqual(MatchRun.objects.count(), 0)
