"""W3-3: Django admin tidak boleh bisa menghapus objek inti.

Delete bawaan admin membypass guard integritas aplikasi
(revert_late_settlements, _locking_batches, konsumsi batch) → state korup.
Hapus HARUS lewat UI aplikasi yang menjalankan guard + revert. Hasil mesin
(MatchResult/MatchRun/ReconBatch) juga tak boleh dibuat manual dari admin.
"""
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase

from reconciliation.admin import (
    MatchResultAdmin, MatchRunAdmin, ReconBatchAdmin, ReviewActionAdmin,
)
from reconciliation.models import MatchResult, MatchRun, ReconBatch, ReviewAction
from sources.admin import UploadAdmin
from sources.models import Upload
from transactions.admin import TransactionAdmin
from transactions.models import Transaction


class AdminHardeningTests(TestCase):
    def setUp(self):
        User = get_user_model()
        su = User.objects.create_superuser("root", "r@r.co", "pw12345")
        self.req = RequestFactory().get("/admin/")
        self.req.user = su
        self.site = AdminSite()

    def _admin(self, cls, model):
        return cls(model, self.site)

    def test_delete_dimatikan_semua_model_inti(self):
        for cls, model in [
            (ReconBatchAdmin, ReconBatch),
            (MatchRunAdmin, MatchRun),
            (MatchResultAdmin, MatchResult),
            (ReviewActionAdmin, ReviewAction),
            (TransactionAdmin, Transaction),
            (UploadAdmin, Upload),
        ]:
            adm = self._admin(cls, model)
            self.assertFalse(
                adm.has_delete_permission(self.req),
                f"{cls.__name__}: delete admin harus mati (bypass guard aplikasi)",
            )

    def test_add_dimatikan_untuk_hasil_mesin(self):
        for cls, model in [
            (ReconBatchAdmin, ReconBatch),
            (MatchRunAdmin, MatchRun),
            (MatchResultAdmin, MatchResult),
        ]:
            adm = self._admin(cls, model)
            self.assertFalse(
                adm.has_add_permission(self.req),
                f"{cls.__name__}: hasil mesin, bukan entri manual",
            )

    def test_change_dimatikan_semua_bukti(self):
        """W6-7a: bukti finansial & hasil mesin view-only — edit dari admin
        membypass guard aplikasi + audit trail."""
        for cls, model in [
            (ReconBatchAdmin, ReconBatch),
            (MatchRunAdmin, MatchRun),
            (MatchResultAdmin, MatchResult),
            (ReviewActionAdmin, ReviewAction),
            (TransactionAdmin, Transaction),
            (UploadAdmin, Upload),
        ]:
            adm = self._admin(cls, model)
            self.assertFalse(
                adm.has_change_permission(self.req),
                f"{cls.__name__}: bukti tak boleh diedit dari admin",
            )

    def test_add_dimatikan_semua_bukti(self):
        for cls, model in [
            (ReviewActionAdmin, ReviewAction),
            (TransactionAdmin, Transaction),
            (UploadAdmin, Upload),
        ]:
            adm = self._admin(cls, model)
            self.assertFalse(
                adm.has_add_permission(self.req),
                f"{cls.__name__}: bukti tak boleh dibuat manual dari admin",
            )

    def test_konfigurasi_tak_bisa_dihapus_tapi_boleh_diedit(self):
        """W6-7b: hapus SourceType (CASCADE ColumnTemplate seed → parser rusak
        senyap), Account (SET_NULL grouping berubah), ToleranceProfile (profil
        Default hilang → reconcile 404) dimatikan; edit/tambah tetap boleh."""
        from reconciliation.admin import ToleranceProfileAdmin
        from reconciliation.models import ToleranceProfile
        from sources.admin import AccountAdmin, SourceTypeAdmin
        from sources.models import Account, SourceType

        for cls, model in [
            (SourceTypeAdmin, SourceType),
            (AccountAdmin, Account),
            (ToleranceProfileAdmin, ToleranceProfile),
        ]:
            adm = self._admin(cls, model)
            self.assertFalse(
                adm.has_delete_permission(self.req),
                f"{cls.__name__}: hapus config merusak seed/relasi",
            )
            self.assertTrue(adm.has_change_permission(self.req))
            self.assertTrue(adm.has_add_permission(self.req))


class AdminHapusRunCliTests(TestCase):
    """W6-7c: MatchRun CLI (batch-null) BOLEH dihapus dari admin — tanpa ini
    pesan guard upload 'hapus run-nya dulu' jalan buntu (alur web tak punya UI
    hapus run CLI). Run milik batch tetap terkunci."""

    def setUp(self):
        from reconciliation.models import ToleranceProfile

        User = get_user_model()
        self.su = User.objects.create_superuser("root", "r@r.co", "pw12345")
        self.req = RequestFactory().get("/admin/")
        self.req.user = self.su
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.run_cli = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol
        )
        from sources.models import Toko

        batch = ReconBatch.objects.create(
            toko=Toko.objects.get(key="lbs"), tolerance=self.tol
        )
        self.run_batch = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=batch
        )

    def test_permission_per_objek(self):
        from django.contrib import admin as dj_admin

        adm = dj_admin.site._registry[MatchRun]
        self.assertFalse(adm.has_delete_permission(self.req))          # bulk: mati
        self.assertTrue(adm.has_delete_permission(self.req, self.run_cli))
        self.assertFalse(adm.has_delete_permission(self.req, self.run_batch))

    def test_hapus_run_cli_lewat_admin_beserta_hasilnya(self):
        # Hasil run CLI ikut cascade — get_deleted_objects memeriksa permission
        # per objek terkait, jadi MatchResultAdmin juga harus mengizinkan.
        MatchResult.objects.create(
            run=self.run_cli, bucket=MatchResult.Bucket.TIDAK,
            score=0, reason_code="no_money",
        )
        self.client.force_login(self.su)
        from django.urls import reverse

        url = reverse("admin:reconciliation_matchrun_delete", args=[self.run_cli.pk])
        resp = self.client.post(url, {"post": "yes"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(MatchRun.objects.filter(pk=self.run_cli.pk).exists())
        self.assertFalse(MatchResult.objects.filter(run_id=self.run_cli.pk).exists())

    def test_hapus_run_batch_lewat_admin_ditolak(self):
        self.client.force_login(self.su)
        from django.urls import reverse

        url = reverse("admin:reconciliation_matchrun_delete", args=[self.run_batch.pk])
        resp = self.client.post(url, {"post": "yes"})
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(MatchRun.objects.filter(pk=self.run_batch.pk).exists())
