"""W1-6: guard review manual.

a) mark_matched pada hasil TANPA uang pasangan (right None) ditolak —
   cocok tanpa uang = akuntansi bohong.
b) override apa pun pada hasil no_money yang left-nya masih AKTIF →
   left dikonsumsi ke batch asal (tidak di-carry ulang / di-match ganda).
c) mark_unmatched pada hasil BERPASANGAN → uangnya dibebaskan (bisa
   di-match ulang); pasangan tetap tercatat utk audit. Predicate
   "berpasangan" di batch_detail/batch_uang exclude bucket TIDAK supaya
   uang yang ditolak muncul lagi sebagai "Uang tanpa pasangan".
"""
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from reconciliation.models import (
    MatchResult,
    MatchRun,
    ReconBatch,
    ReviewAction,
    ToleranceProfile,
)
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction


class _Base(TestCase):
    def setUp(self):
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.toko = Toko.objects.get(key="lbs")
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.up_p = Upload.objects.create(
            source_type=self.panel, toko=self.toko, original_name="P.xlsx"
        )
        self.up_b = Upload.objects.create(
            source_type=self.bank, toko=self.toko, original_name="B.csv"
        )
        self.batch = ReconBatch.objects.create(
            toko=self.toko, tolerance=self.tol, recon_date=date(2026, 6, 27)
        )
        self.run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=self.batch
        )
        self._n = 0

    def _tx(self, st, up, amount, dt=datetime(2026, 6, 27, 10), **kw):
        self._n += 1
        return Transaction.objects.create(
            upload=up, source_type=st, toko=self.toko, jenis="depo",
            amount=Decimal(amount), money_delta=Decimal(amount),
            occurred_at=dt, row_hash=f"h{self._n}", **kw,
        )

    def _panel(self, amount="50000", **kw):
        return self._tx(self.panel, self.up_p, amount, **kw)

    def _bank(self, amount="50000", **kw):
        return self._tx(self.bank, self.up_b, amount, **kw)

    def _no_money(self, left):
        return MatchResult.objects.create(
            run=self.run, bucket=MatchResult.Bucket.TIDAK, left=left, right=None,
            score=0, reason_code="no_money",
        )

    def _pair(self, left, right, bucket=MatchResult.Bucket.TINJAU,
              reason="name_partial"):
        return MatchResult.objects.create(
            run=self.run, bucket=bucket, left=left, right=right,
            score=80, reason_code=reason,
        )


class TolakCocokTanpaUangTests(_Base):
    def test_single_mark_matched_tanpa_uang_ditolak(self):
        r = self._no_money(self._panel())
        resp = self.client.post(reverse("review", args=[r.pk]),
                                {"action": "mark_matched"})
        self.assertEqual(resp.status_code, 400)
        r.refresh_from_db()
        self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)
        self.assertEqual(r.reason_code, "no_money")
        self.assertFalse(ReviewAction.objects.filter(result=r).exists())

    def test_bulk_mark_matched_tanpa_uang_dilewati(self):
        r_pair = self._pair(self._panel(), self._bank())
        r_nomoney = self._no_money(self._panel("60000"))
        resp = self.client.post(
            reverse("bulk_review", args=[self.run.pk]),
            {"action": "mark_matched",
             "result_ids": [str(r_pair.pk), str(r_nomoney.pk)]},
            follow=True,
        )
        r_pair.refresh_from_db()
        r_nomoney.refresh_from_db()
        self.assertEqual(r_pair.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(r_nomoney.bucket, MatchResult.Bucket.TIDAK)  # dilewati
        self.assertEqual(r_nomoney.reason_code, "no_money")
        self.assertFalse(ReviewAction.objects.filter(result=r_nomoney).exists())
        self.assertTrue(ReviewAction.objects.filter(result=r_pair).exists())
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("dilewati" in m for m in msgs), msgs)


class KonsumsiNoMoneyAktifTests(_Base):
    def test_mark_review_no_money_mengonsumsi_left_ke_batch_asal(self):
        p = self._panel()
        r = self._no_money(p)
        resp = self.client.post(reverse("review", args=[r.pk]),
                                {"action": "mark_review"})
        self.assertEqual(resp.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.consumed_by_batch, self.batch)

    def test_mark_unmatched_no_money_mengonsumsi_left(self):
        p = self._panel()
        r = self._no_money(p)
        self.client.post(reverse("review", args=[r.pk]),
                         {"action": "mark_unmatched"})
        p.refresh_from_db()
        self.assertEqual(p.consumed_by_batch, self.batch)

    def test_left_sudah_dikonsumsi_batch_lain_tak_disentuh(self):
        other = ReconBatch.objects.create(toko=self.toko, tolerance=self.tol)
        p = self._panel()
        p.consumed_by_batch = other
        p.save(update_fields=["consumed_by_batch"])
        r = self._no_money(p)
        self.client.post(reverse("review", args=[r.pk]),
                         {"action": "mark_review"})
        p.refresh_from_db()
        self.assertEqual(p.consumed_by_batch, other)

    def test_bulk_mark_review_no_money_mengonsumsi_left(self):
        p = self._panel()
        r = self._no_money(p)
        self.client.post(reverse("bulk_review", args=[self.run.pk]),
                         {"action": "mark_review", "result_ids": [str(r.pk)]})
        p.refresh_from_db()
        self.assertEqual(p.consumed_by_batch, self.batch)


class BebaskanUangMarkUnmatchedTests(_Base):
    def test_mark_unmatched_membebaskan_uang_batch_hasil(self):
        p, m = self._panel(), self._bank()
        m.consumed_by_batch = self.batch
        m.save(update_fields=["consumed_by_batch"])
        r = self._pair(p, m, bucket=MatchResult.Bucket.COCOK, reason="amount+date+name")
        self.client.post(reverse("review", args=[r.pk]),
                         {"action": "mark_unmatched"})
        m.refresh_from_db()
        r.refresh_from_db()
        self.assertIsNone(m.consumed_by_batch)          # bebas — bisa match ulang
        self.assertEqual(r.right_id, m.id)               # jejak audit tetap
        self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)

    def test_mark_unmatched_uang_resolved_by_batch_juga_dibebaskan(self):
        # Late settlement: hasil milik batch asal, uangnya dikonsumsi batch resolver.
        resolver = ReconBatch.objects.create(
            toko=self.toko, tolerance=self.tol, recon_date=date(2026, 6, 28)
        )
        p, m = self._panel(), self._bank()
        m.consumed_by_batch = resolver
        m.save(update_fields=["consumed_by_batch"])
        r = self._pair(p, m, bucket=MatchResult.Bucket.COCOK, reason="late_settlement")
        r.resolved_by_batch = resolver
        r.save(update_fields=["resolved_by_batch"])
        self.client.post(reverse("review", args=[r.pk]),
                         {"action": "mark_unmatched"})
        m.refresh_from_db()
        self.assertIsNone(m.consumed_by_batch)

    def test_mark_unmatched_uang_batch_lain_ikut_dibebaskan(self):
        """W6-5c — KONTRAK BERUBAH SADAR dari W1-6c: dulu uang yang dikonsumsi
        batch LAIN tak disentuh; kini dibebaskan apa pun batch-nya. Kasus retro:
        hasil ditulis di run batch HOME sedangkan uangnya dikonsumsi batch
        BERJALAN (provenance tak tersimpan) — guard lama membuat pembebasan
        mustahil. Aman: pasangan DITOLAK reviewer & summary kedua batch
        di-refresh."""
        other = ReconBatch.objects.create(toko=self.toko, tolerance=self.tol)
        p, m = self._panel(), self._bank()
        m.consumed_by_batch = other
        m.save(update_fields=["consumed_by_batch"])
        r = self._pair(p, m, bucket=MatchResult.Bucket.COCOK)
        self.client.post(reverse("review", args=[r.pk]),
                         {"action": "mark_unmatched"})
        m.refresh_from_db()
        self.assertIsNone(m.consumed_by_batch)
        # Summary batch hasil DAN batch yang tadinya mengonsumsi ikut segar.
        other.refresh_from_db()
        self.batch.refresh_from_db()
        self.assertIn("buckets", other.summary)
        self.assertIn("buckets", self.batch.summary)


class TolakCocokSatuSisiTests(_Base):
    """W6-5a: mark_matched wajib DUA sisi (left DAN right) — hasil no_panel
    (left None) yang ditandai cocok jadi COCOK hantu yang tak terhitung
    _matched_money. Tombol 'Tandai Cocok' juga disembunyikan di UI."""

    def _no_panel(self):
        m = self._bank()
        return MatchResult.objects.create(
            run=self.run, bucket=MatchResult.Bucket.TIDAK, left=None, right=m,
            score=0, reason_code="no_panel",
        )

    def test_single_mark_matched_tanpa_left_ditolak(self):
        r = self._no_panel()
        resp = self.client.post(reverse("review", args=[r.pk]),
                                {"action": "mark_matched"})
        self.assertEqual(resp.status_code, 400)
        r.refresh_from_db()
        self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)
        self.assertFalse(ReviewAction.objects.filter(result=r).exists())

    def test_bulk_mark_matched_tanpa_left_dilewati(self):
        r = self._no_panel()
        resp = self.client.post(
            reverse("bulk_review", args=[self.run.pk]),
            {"action": "mark_matched", "result_ids": [str(r.pk)]},
            follow=True,
        )
        r.refresh_from_db()
        self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)
        self.assertFalse(ReviewAction.objects.filter(result=r).exists())
        msgs = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("dilewati" in m for m in msgs), msgs)

    def test_tombol_cocok_disembunyikan_untuk_hasil_satu_sisi(self):
        self._no_panel()
        resp = self.client.get(reverse("run_detail", args=[self.run.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'title="Tandai Cocok"')
        self.assertContains(resp, 'title="Perlu Ditinjau"')

    def test_tombol_cocok_tampil_untuk_hasil_dua_sisi(self):
        self._pair(self._panel(), self._bank())
        resp = self.client.get(reverse("run_detail", args=[self.run.pk]))
        self.assertContains(resp, 'title="Tandai Cocok"')


class ReviewAtomikTests(_Base):
    """W6-5b: review & bulk_review atomic menyeluruh — gagal di tengah
    (mis. refresh summary) me-rollback SEMUA mutasi, tak ada hasil setengah
    tertulis yang menyimpang dari summary."""

    def test_review_gagal_refresh_rollback_semua(self):
        from unittest.mock import patch

        p, m = self._panel(), self._bank()
        r = self._pair(p, m)
        with patch("web.views.refresh_batch_summary",
                   side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("review", args=[r.pk]),
                                 {"action": "mark_matched"})
        r.refresh_from_db()
        self.assertEqual(r.bucket, MatchResult.Bucket.TINJAU)  # tak berubah
        self.assertFalse(ReviewAction.objects.filter(result=r).exists())

    def test_bulk_gagal_refresh_rollback_semua(self):
        from unittest.mock import patch

        p, m = self._panel(), self._bank()
        r = self._pair(p, m)
        with patch("web.views.refresh_batch_summary",
                   side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("bulk_review", args=[self.run.pk]),
                    {"action": "mark_matched", "result_ids": [str(r.pk)]},
                )
        r.refresh_from_db()
        self.assertEqual(r.bucket, MatchResult.Bucket.TINJAU)
        self.assertFalse(ReviewAction.objects.filter(result=r).exists())


class PredikatBerpasanganTests(_Base):
    def _uang_ditolak(self):
        """Uang dikonsumsi batch, pasangannya DITOLAK (bucket TIDAK) — harus
        tampil lagi sebagai 'Uang tanpa pasangan'."""
        p, m = self._panel(), self._bank()
        m.consumed_by_batch = self.batch
        m.save(update_fields=["consumed_by_batch"])
        self._pair(p, m, bucket=MatchResult.Bucket.TIDAK, reason="manual_override")
        return m

    def test_batch_uang_menampilkan_uang_pasangan_ditolak(self):
        m = self._uang_ditolak()
        resp = self.client.get(reverse("batch_uang", args=[self.batch.pk]))
        self.assertEqual(resp.status_code, 200)
        ids = [t.id for t in resp.context["page"].object_list]
        self.assertIn(m.id, ids)

    def test_batch_detail_per_bank_hitung_pasangan_ditolak_sbg_unpaired(self):
        self._uang_ditolak()
        resp = self.client.get(reverse("batch_detail", args=[self.batch.pk]))
        self.assertEqual(resp.status_code, 200)
        per_bank = resp.context["per_bank"]
        self.assertEqual(len(per_bank), 1)
        self.assertEqual(per_bank[0]["unpaired"], 1)
        self.assertEqual(per_bank[0]["paired"], 0)
