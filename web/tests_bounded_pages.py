"""W4-7 — halaman berbatas: dropdown mutasi 60 file, riwayat reconcile slice DB,
dashboard hanya memuat batch 60 hari; data lama tetap terjangkau (URL/kartu)."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from reconciliation.models import ReconBatch, ToleranceProfile
from sources.models import SourceType, Toko, Upload

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        User.objects.create_user("adm", password="pw123456", role="admin")
        self.client.login(username="adm", password="pw123456")
        self.lbs = Toko.objects.get(key="lbs")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})


class MutasiDropdownCapTests(_Base):
    def test_dropdown_max_60_file_lama_via_url_tetap_bisa(self):
        ups = [
            Upload.objects.create(
                source_type=self.bank, toko=self.lbs, original_name=f"f{i:03d}.csv"
            )
            for i in range(65)
        ]
        r = self.client.get(reverse("bank_mutations"))
        self.assertEqual(len(r.context["uploads"]), 60)
        self.assertContains(r, "f064.csv")      # terbaru tampil
        self.assertNotContains(r, "f000.csv")   # tertua terpangkas dari dropdown
        # file di luar cap tetap bisa dipilih via URL — disisipkan & teranotasi
        tua = ups[0]
        r = self.client.get(reverse("bank_mutations"), {"upload": tua.id})
        self.assertEqual(r.context["sel_upload"].id, tua.id)
        self.assertIn(tua.id, [u.id for u in r.context["uploads"]])
        self.assertTrue(hasattr(r.context["sel_upload"], "n_rows_file"))


class ReconcileRiwayatSliceTests(_Base):
    def test_hanya_20_terbaru_nomor_tetap_posisi_asli(self):
        for _ in range(25):
            ReconBatch.objects.create(toko=self.lbs, tolerance=self.tol)
        r = self.client.get(reverse("reconcile"))
        self.assertEqual(len(r.context["batches"]), 20)
        self.assertContains(r, ">#25</a>")
        self.assertContains(r, ">#6</a>")
        self.assertNotContains(r, ">#5</a>")  # di luar halaman 1

    def test_halaman_2_menjangkau_batch_lama(self):
        """W6-8b: riwayat batch berpaginasi (?hal=, 20/halaman) — dulu hard-stop
        20 tanpa pager sehingga batch lama tak terjangkau dari UI."""
        for _ in range(25):
            ReconBatch.objects.create(toko=self.lbs, tolerance=self.tol)
        r1 = self.client.get(reverse("reconcile"))
        self.assertContains(r1, "hal=2")  # pager tampil
        r2 = self.client.get(reverse("reconcile"), {"hal": "2"})
        self.assertEqual(len(r2.context["batches"]), 5)
        self.assertContains(r2, ">#5</a>")
        self.assertContains(r2, ">#1</a>")
        self.assertNotContains(r2, ">#25</a>")


class DashboardBoundedTests(_Base):
    def test_batch_purba_tetap_jadi_kartu_terakhir(self):
        tua = ReconBatch.objects.create(
            toko=self.lbs, tolerance=self.tol,
            recon_date=date.today() - timedelta(days=100),
        )
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.context["last"].id, tua.id)  # kartu status tetap benar
        # kalender 14 hari tak memuatnya (di luar jendela — perilaku lama juga)
        self.assertTrue(all(c["batch"] is None for c in r.context["kal"]))

    def test_batch_baru_masuk_kalender(self):
        b = ReconBatch.objects.create(
            toko=self.lbs, tolerance=self.tol, recon_date=date.today()
        )
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.context["last"].id, b.id)
        self.assertIn(b.id, [c["batch"].id for c in r.context["kal"] if c["batch"]])
