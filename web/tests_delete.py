from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction

User = get_user_model()


def _mk_upload(toko):
    # Tanpa file fisik: Upload.file memang TIDAK pernah diisi di alur nyata
    # (ingest hanya menyimpan baris hasil parse, file asli dibuang usai parse).
    st = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
    up = Upload.objects.create(source_type=st, toko=toko, original_name="f.xlsx")
    return up, st


class DeleteUploadTests(TestCase):
    def setUp(self):
        self.lbs = Toko.objects.get(key="lbs")
        User.objects.create_user("adm", password="pw123456", role="admin")

    def test_admin_hapus_upload_beserta_tx(self):
        from datetime import datetime
        from decimal import Decimal
        up, st = _mk_upload(self.lbs)
        Transaction.objects.create(
            upload=up, source_type=st, toko=self.lbs, jenis="depo",
            amount=Decimal("1"), money_delta=Decimal("1"),
            occurred_at=datetime(2026, 6, 27, 10, 0), row_hash="del-1",
        )
        self.client.login(username="adm", password="pw123456")
        r = self.client.post(reverse("delete_upload", args=[up.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Upload.objects.filter(pk=up.pk).exists())
        self.assertEqual(Transaction.objects.count(), 0)

    def test_auditor_ditolak(self):
        up, _ = _mk_upload(self.lbs)
        aud = User.objects.create_user("aud1", password="pw123456", role="auditor")
        aud.allowed_tokos.add(self.lbs)
        self.client.login(username="aud1", password="pw123456")
        self.client.post(reverse("delete_upload", args=[up.pk]))
        self.assertTrue(Upload.objects.filter(pk=up.pk).exists())

    def test_get_tidak_menghapus(self):
        up, _ = _mk_upload(self.lbs)
        self.client.login(username="adm", password="pw123456")
        self.client.get(reverse("delete_upload", args=[up.pk]))
        self.assertTrue(Upload.objects.filter(pk=up.pk).exists())

    def test_tombol_hanya_untuk_admin(self):
        # Hapus (massal, checkbox pilih-semua) hanya untuk admin.
        _mk_upload(self.lbs)
        aud = User.objects.create_user("aud2", password="pw123456", role="auditor")
        aud.allowed_tokos.add(self.lbs)
        self.client.login(username="aud2", password="pw123456")
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})
        r = self.client.get(reverse("upload"))
        self.assertNotContains(r, "Hapus terpilih")
        self.assertNotContains(r, 'id="chkAll"')
        self.client.login(username="adm", password="pw123456")
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})
        r = self.client.get(reverse("upload"))
        self.assertContains(r, "Hapus terpilih")
        self.assertContains(r, 'id="chkAll"')


class DeleteBatchTests(TestCase):
    def setUp(self):
        from reconciliation.engine import run_batch
        from reconciliation.models import ToleranceProfile
        self.lbs = Toko.objects.get(key="lbs")
        tol = ToleranceProfile.objects.get_or_create(name="Default", defaults={"date_window_days": 1})[0]
        self.batch = run_batch(self.lbs, tol)
        User.objects.create_user("adm", password="pw123456", role="admin")

    def test_admin_hapus_batch_transaksi_utuh(self):
        from datetime import datetime
        from decimal import Decimal
        from reconciliation.models import MatchRun, ReconBatch
        st = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        up = Upload.objects.create(source_type=st, toko=self.lbs)
        Transaction.objects.create(
            upload=up, source_type=st, toko=self.lbs, jenis="depo",
            amount=Decimal("1"), money_delta=Decimal("1"),
            occurred_at=datetime(2026, 6, 27, 10, 0), row_hash="keep-1",
        )
        self.client.login(username="adm", password="pw123456")
        r = self.client.post(reverse("delete_batch", args=[self.batch.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(ReconBatch.objects.filter(pk=self.batch.pk).exists())
        self.assertEqual(MatchRun.objects.filter(batch_id=self.batch.pk).count(), 0)
        self.assertEqual(Transaction.objects.count(), 1)  # transaksi TIDAK ikut terhapus

    def test_supervisor_ditolak(self):
        from reconciliation.models import ReconBatch
        User.objects.create_user("sup", password="pw123456", role="supervisor")
        self.client.login(username="sup", password="pw123456")
        self.client.post(reverse("delete_batch", args=[self.batch.pk]))
        self.assertTrue(ReconBatch.objects.filter(pk=self.batch.pk).exists())
