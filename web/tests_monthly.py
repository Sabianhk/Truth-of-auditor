"""Ringkasan Bulanan — rekap ReconBatch.summary per tanggal dalam satu bulan."""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from reconciliation.models import ReconBatch, ToleranceProfile
from sources.models import Toko
from web.monthly import monthly_summary


def _summary(dp_panel, dp_uang, wd_panel, wd_uang, cocok, tinjau, tidak):
    return {
        "dp": {"panel": dp_panel, "money_gross": dp_uang, "money_matched": dp_uang,
               "money": dp_uang, "selisih": dp_panel - dp_uang},
        "wd": {"panel": wd_panel, "money_gross": wd_uang, "money_matched": wd_uang,
               "money": wd_uang, "selisih": wd_panel - wd_uang},
        "buckets": {"cocok": cocok, "perlu_tinjau": tinjau, "tidak_cocok": tidak},
    }


class _Data(TestCase):
    def setUp(self):
        self.toko = Toko.objects.get(key="lbs")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]

    def batch(self, d, summary):
        return ReconBatch.objects.create(
            toko=self.toko, tolerance=self.tol, recon_date=d, summary=summary
        )


class MonthlySummaryTests(_Data):
    def test_baris_per_tanggal_dan_total(self):
        self.batch(date(2026, 6, 27), _summary(100, 90, 200, 200, 50, 3, 2))
        self.batch(date(2026, 6, 28), _summary(60, 60, 40, 30, 20, 0, 1))
        data = monthly_summary(self.toko, 2026, 6)
        self.assertEqual(len(data["rows"]), 2)
        r0 = data["rows"][0]
        self.assertEqual(r0["date"], date(2026, 6, 27))
        self.assertEqual(r0["dp_panel"], 100)
        self.assertEqual(r0["dp_uang"], 90)
        self.assertEqual(r0["dp_selisih"], 10)
        self.assertEqual(r0["wd_selisih"], 0)
        self.assertEqual(r0["cocok"], 50)
        self.assertEqual(r0["tinjau"], 3)
        self.assertEqual(r0["tidak"], 2)
        self.assertIsNotNone(r0["batch_id"])
        t = data["total"]
        self.assertEqual(t["dp_panel"], 160)
        self.assertEqual(t["dp_uang"], 150)
        self.assertEqual(t["wd_panel"], 240)
        self.assertEqual(t["wd_uang"], 230)
        self.assertEqual(t["cocok"], 70)
        self.assertEqual(t["tinjau"], 3)
        self.assertEqual(t["tidak"], 3)

    def test_urut_tanggal_menaik(self):
        self.batch(date(2026, 6, 28), _summary(1, 1, 0, 0, 1, 0, 0))
        self.batch(date(2026, 6, 27), _summary(2, 2, 0, 0, 2, 0, 0))
        rows = monthly_summary(self.toko, 2026, 6)["rows"]
        self.assertEqual([r["date"].day for r in rows], [27, 28])

    def test_hanya_bulan_dan_toko_diminta(self):
        self.batch(date(2026, 6, 27), _summary(1, 1, 0, 0, 1, 0, 0))
        self.batch(date(2026, 7, 1), _summary(9, 9, 0, 0, 9, 0, 0))  # bulan lain
        toko2 = Toko.objects.exclude(pk=self.toko.pk).first()
        ReconBatch.objects.create(
            toko=toko2, tolerance=self.tol, recon_date=date(2026, 6, 27),
            summary=_summary(5, 5, 0, 0, 5, 0, 0),
        )
        data = monthly_summary(self.toko, 2026, 6)
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["dp_panel"], 1)

    def test_summary_kosong_tak_meledak(self):
        self.batch(date(2026, 6, 27), {})
        data = monthly_summary(self.toko, 2026, 6)
        self.assertEqual(data["rows"][0]["dp_panel"], 0)
        self.assertEqual(data["rows"][0]["cocok"], 0)

    def test_bulan_tanpa_batch_kosong(self):
        data = monthly_summary(self.toko, 2026, 6)
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["total"]["dp_panel"], 0)


class MonthlyViewTests(_Data):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        self.client.post(reverse("set_toko"), {"toko_id": self.toko.id})

    def test_butuh_login(self):
        self.client.logout()
        r = self.client.get(reverse("monthly_overview"))
        self.assertEqual(r.status_code, 302)

    def test_default_bulan_batch_terakhir(self):
        self.batch(date(2026, 5, 3), _summary(1, 1, 0, 0, 1, 0, 0))
        self.batch(date(2026, 6, 27), _summary(100, 90, 0, 0, 50, 0, 0))
        r = self.client.get(reverse("monthly_overview"))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Ringkasan Bulanan", html)
        self.assertIn("27 Jun", html)  # bulan Juni jadi default (batch terbaru)
        self.assertNotIn("03 Mei", html)

    def test_pilih_bulan_via_query(self):
        self.batch(date(2026, 5, 3), _summary(7, 7, 0, 0, 7, 0, 0))
        self.batch(date(2026, 6, 27), _summary(100, 90, 0, 0, 50, 0, 0))
        r = self.client.get(reverse("monthly_overview"), {"month": "2026-05"})
        html = r.content.decode()
        self.assertIn("03 Mei", html)
        self.assertNotIn("27 Jun", html)

    def test_empty_state(self):
        r = self.client.get(reverse("monthly_overview"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Belum ada rekonsiliasi", r.content.decode())

    def test_bulan_di_luar_rentang_fallback(self):
        """W3-6a: ?month=2026-13 dulu ValueError 500 (date(year, 13, 1)) —
        kini fallback ke bulan valid terakhir."""
        self.batch(date(2026, 6, 27), _summary(100, 90, 0, 0, 50, 0, 0))
        for buruk in ("2026-13", "2026-00", "0000-01"):
            r = self.client.get(reverse("monthly_overview"), {"month": buruk})
            self.assertEqual(r.status_code, 200, buruk)
            self.assertIn("27 Jun", r.content.decode(), buruk)
