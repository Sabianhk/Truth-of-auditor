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
