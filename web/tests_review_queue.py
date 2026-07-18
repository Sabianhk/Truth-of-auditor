"""Halaman /tinjau/ (Area Pengecekan): hasil perlu-dicek lintas run untuk toko
aktif — tab perlu_tinjau / tidak_cocok / tidak_ada_panel + summary, dengan RBAC."""
from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from reconciliation.models import MatchResult, MatchRun, ReconBatch, ToleranceProfile
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction

User = get_user_model()
_seq = iter(range(1, 100000))


class ReviewQueueTests(TestCase):
    def setUp(self):
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        self.lbs = Toko.objects.get(key="lbs")
        self.slo = Toko.objects.get(key="slo")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})

    def _tinjau(self, toko, ticket):
        up = Upload.objects.create(source_type=self.panel, toko=toko)
        batch = ReconBatch.objects.create(toko=toko, tolerance=self.tol)
        run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=batch
        )
        left = Transaction.objects.create(
            upload=up, source_type=self.panel, toko=toko, jenis="depo",
            amount=Decimal("50000"), occurred_at=datetime(2026, 6, 27, 10, 0),
            ticket_no=ticket, row_hash=f"q-{next(_seq)}", raw={},
        )
        return MatchResult.objects.create(
            run=run, bucket=MatchResult.Bucket.TINJAU, reason_code="amount_mismatch", left=left,
        )

    def test_hanya_bucket_tinjau_toko_aktif(self):
        self._tinjau(self.lbs, "D-LBS")
        self._tinjau(self.slo, "D-SLO")
        r = self.client.get(reverse("review_queue"))
        self.assertContains(r, "D-LBS")
        self.assertNotContains(r, "D-SLO")

    def test_kosong_empty_state(self):
        r = self.client.get(reverse("review_queue"))
        self.assertContains(r, "Tidak ada hasil")

    def test_rbac_auditor_toko_lain(self):
        User.objects.create_user("a2", "a2@a.co", "pw12345", role="auditor")
        u = User.objects.get(username="a2")
        u.allowed_tokos.set([self.slo])
        self._tinjau(self.lbs, "D-LBS")
        self.client.logout()
        self.client.login(username="a2", password="pw12345")
        self.client.post(reverse("set_toko"), {"toko_id": self.slo.id})
        r = self.client.get(reverse("review_queue"))
        self.assertNotContains(r, "D-LBS")

    def test_aksi_review_dari_antrean(self):
        res = self._tinjau(self.lbs, "D-LBS")
        # Guard W1-6a: mark_matched butuh pasangan uang — beri right.
        bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        up_b = Upload.objects.create(source_type=bank, toko=self.lbs)
        res.right = Transaction.objects.create(
            upload=up_b, source_type=bank, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 27, 11, 0), row_hash=f"q-{next(_seq)}",
        )
        res.save(update_fields=["right"])
        r = self.client.post(
            reverse("review", args=[res.pk]),
            {"action": "mark_matched", "show_run_col": "1"},
        )
        self.assertEqual(r.status_code, 200)
        res.refresh_from_db()
        self.assertEqual(res.bucket, MatchResult.Bucket.COCOK)


class ReviewQueueFilterTests(ReviewQueueTests):
    """Filter antrean tinjau: DP/WD, rentang tanggal transaksi, bank pemain,
    bank title — pola sama seperti run_detail."""

    def _tinjau2(self, *, ticket, jenis="depo", dt=datetime(2026, 6, 27, 10, 0),
                 player_bank="", bank_title=""):
        up = Upload.objects.create(source_type=self.panel, toko=self.lbs)
        batch = ReconBatch.objects.create(toko=self.lbs, tolerance=self.tol)
        run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=batch
        )
        left = Transaction.objects.create(
            upload=up, source_type=self.panel, toko=self.lbs, jenis=jenis,
            amount=Decimal("50000"), occurred_at=dt, ticket_no=ticket,
            player_bank=player_bank, bank_title=bank_title,
            row_hash=f"q-{next(_seq)}", raw={},
        )
        return MatchResult.objects.create(
            run=run, bucket=MatchResult.Bucket.TINJAU,
            reason_code="amount_mismatch", left=left,
        )

    def test_filter_flow_dp_wd(self):
        self._tinjau2(ticket="D-DEPO", jenis="depo")
        self._tinjau2(ticket="W-WD", jenis="wd")
        r = self.client.get(reverse("review_queue"), {"flow": "wd"})
        self.assertContains(r, "W-WD")
        self.assertNotContains(r, "D-DEPO")

    def test_filter_rentang_tanggal(self):
        self._tinjau2(ticket="D-27", dt=datetime(2026, 6, 27, 10, 0))
        self._tinjau2(ticket="D-28", dt=datetime(2026, 6, 28, 10, 0))
        r = self.client.get(reverse("review_queue"),
                            {"from": "2026-06-28", "to": "2026-06-28"})
        self.assertContains(r, "D-28")
        self.assertNotContains(r, "D-27")

    def test_filter_bank_pemain(self):
        self._tinjau2(ticket="D-BCA", player_bank="BCA")
        self._tinjau2(ticket="D-DANA", player_bank="DANA")
        r = self.client.get(reverse("review_queue"), {"bank": "DANA"})
        self.assertContains(r, "D-DANA")
        self.assertNotContains(r, "D-BCA")

    def test_filter_bank_title(self):
        self._tinjau2(ticket="D-T1", bank_title="BCA")
        self._tinjau2(ticket="D-T2", bank_title="BRI")
        r = self.client.get(reverse("review_queue"), {"btitle": "BRI"})
        self.assertContains(r, "D-T2")
        self.assertNotContains(r, "D-T1")

    def test_filter_kombinasi_dan_chip_terhitung(self):
        self._tinjau2(ticket="D-X", jenis="wd", player_bank="BCA", bank_title="BCA")
        self._tinjau2(ticket="D-Y", jenis="wd", player_bank="DANA", bank_title="BCA")
        r = self.client.get(reverse("review_queue"), {"flow": "wd", "bank": "BCA"})
        self.assertContains(r, "D-X")
        self.assertNotContains(r, "D-Y")
        # chip bank pemain tetap menawarkan DANA (dihitung dalam flow terpilih)
        self.assertContains(r, "DANA")


class AreaPengecekanTests(ReviewQueueTests):
    """Gelombang UAT: rename 'Area Pengecekan' + tab tidak_cocok / tidak_ada_panel
    + ringkasan total di bawah tabel."""

    def _hasil(self, *, bucket, ticket="", right_amount=None, reason="x"):
        """MatchResult bebas bucket. ticket -> ada sisi kiri; right_amount -> ada sisi uang."""
        up = Upload.objects.create(source_type=self.panel, toko=self.lbs)
        batch = ReconBatch.objects.create(toko=self.lbs, tolerance=self.tol)
        run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=batch
        )
        left = right = None
        if ticket:
            left = Transaction.objects.create(
                upload=up, source_type=self.panel, toko=self.lbs, jenis="depo",
                amount=Decimal("50000"), occurred_at=datetime(2026, 6, 27, 10, 0),
                ticket_no=ticket, row_hash=f"q-{next(_seq)}", raw={},
            )
        if right_amount is not None:
            bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
            upb = Upload.objects.create(source_type=bank, toko=self.lbs)
            right = Transaction.objects.create(
                upload=upb, source_type=bank, toko=self.lbs, jenis="depo",
                amount=Decimal(right_amount), occurred_at=datetime(2026, 6, 27, 11, 0),
                counterparty="ORPHAN GUY", row_hash=f"q-{next(_seq)}", raw={},
            )
        return MatchResult.objects.create(
            run=run, bucket=bucket, reason_code=reason, left=left, right=right,
        )

    def test_heading_area_pengecekan(self):
        r = self.client.get(reverse("review_queue"))
        self.assertContains(r, "Area Pengecekan")

    def test_default_hanya_perlu_tinjau(self):
        self._tinjau(self.lbs, "D-TINJAU")
        self._hasil(bucket=MatchResult.Bucket.TIDAK, ticket="D-TIDAK")
        r = self.client.get(reverse("review_queue"))
        self.assertContains(r, "D-TINJAU")
        self.assertNotContains(r, "D-TIDAK")

    def test_tab_tidak_cocok(self):
        self._tinjau(self.lbs, "D-TINJAU")
        self._hasil(bucket=MatchResult.Bucket.TIDAK, ticket="D-TIDAK")
        self._hasil(bucket=MatchResult.Bucket.TIDAK, right_amount="75000", reason="no_panel")
        r = self.client.get(reverse("review_queue"), {"bucket": "tidak_cocok"})
        self.assertContains(r, "D-TIDAK")
        self.assertNotContains(r, "D-TINJAU")
        self.assertNotContains(r, "ORPHAN GUY")  # orphan tak ikut tab tidak_cocok

    def test_tab_tidak_ada_panel(self):
        self._hasil(bucket=MatchResult.Bucket.TIDAK, ticket="D-TIDAK")
        self._hasil(bucket=MatchResult.Bucket.TIDAK, right_amount="75000", reason="no_panel")
        r = self.client.get(reverse("review_queue"), {"bucket": "tidak_ada_panel"})
        self.assertContains(r, "ORPHAN GUY")
        self.assertNotContains(r, "D-TIDAK")

    def test_summary_totbar(self):
        self._tinjau(self.lbs, "D-A")
        self._tinjau(self.lbs, "D-B")
        r = self.client.get(reverse("review_queue"))
        self.assertContains(r, "totbar")
        self.assertContains(r, "100.000")  # 2 x 50.000 kredit (locale id: titik ribuan)

    def test_tab_count_tampil(self):
        self._tinjau(self.lbs, "D-TINJAU")
        self._hasil(bucket=MatchResult.Bucket.TIDAK, right_amount="75000", reason="no_panel")
        r = self.client.get(reverse("review_queue"))
        self.assertContains(r, "Perlu Ditinjau")
        self.assertContains(r, "Tidak Cocok")
        self.assertContains(r, "Tidak Ada di Panel")

    def test_bucket_param_tak_dikenal_fallback_default(self):
        self._tinjau(self.lbs, "D-TINJAU")
        r = self.client.get(reverse("review_queue"), {"bucket": "ngawur"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "D-TINJAU")
