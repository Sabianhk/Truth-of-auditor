"""Guard integritas (F1): hasil rekonsiliasi tidak boleh hidup lebih lama dari buktinya.

Menghapus Upload meng-cascade Transaction → MatchResult.left/right ikut CASCADE,
tapi ReconBatch/MatchRun selamat dengan summary JSON basi (batch "Balanced ✓"
palsu, run dengan stat ribuan tapi tabel kosong). Dua lapis pertahanan:
(A) upload yang buktinya dipakai (MatchResult ATAU consumed_by_batch) tak bisa
dihapus — hapus batch-nya dulu; (B) data lama yang terlanjur cangkang dideteksi
dan diberi banner merah di batch_detail & run_detail.
"""
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from reconciliation.models import MatchResult, MatchRun, ReconBatch, ToleranceProfile
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction

User = get_user_model()

BANNER = "TIDAK BISA diverifikasi"


class _Base(TestCase):
    def setUp(self):
        self.lbs = Toko.objects.get(key="lbs")
        self.tol = ToleranceProfile.objects.get(name="Default")
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.adm = User.objects.create_user("adm", password="pw123456", role="admin")
        self.client.login(username="adm", password="pw123456")
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})
        self._rh = 0

    def _upload(self, source=None, name="file.xlsx"):
        return Upload.objects.create(
            source_type=source or self.panel, toko=self.lbs, original_name=name,
        )

    def _tx(self, up, **kw):
        self._rh += 1
        defaults = dict(
            upload=up, source_type=up.source_type, toko=self.lbs, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 28, 10, 0), row_hash=f"ig{self._rh}",
        )
        defaults.update(kw)
        return Transaction.objects.create(**defaults)

    def _batch(self, summary=None):
        batch = ReconBatch.objects.create(
            toko=self.lbs, tolerance=self.tol,
            date_from=date(2026, 6, 28), date_to=date(2026, 6, 28),
            summary=summary or {},
        )
        run = MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=batch,
            date_from=date(2026, 6, 28), date_to=date(2026, 6, 28),
        )
        return batch, run


class DeleteUploadGuardTests(_Base):
    def test_blokir_bila_tx_jadi_left(self):
        up = self._upload()
        batch, run = self._batch()
        MatchResult.objects.create(run=run, bucket=MatchResult.Bucket.TIDAK,
                                   left=self._tx(up), reason_code="no_money")
        r = self.client.post(reverse("delete_upload", args=[up.pk]), follow=True)
        self.assertTrue(Upload.objects.filter(pk=up.pk).exists())
        self.assertContains(r, "tidak bisa dihapus")
        self.assertContains(r, "#1")  # nomor batch per-toko

    def test_blokir_bila_tx_jadi_right(self):
        up = self._upload(source=self.bank)
        batch, run = self._batch()
        MatchResult.objects.create(run=run, bucket=MatchResult.Bucket.COCOK,
                                   right=self._tx(up), reason_code="")
        self.client.post(reverse("delete_upload", args=[up.pk]))
        self.assertTrue(Upload.objects.filter(pk=up.pk).exists())

    def test_blokir_bila_hanya_dikonsumsi_batch(self):
        up = self._upload(source=self.bank)
        batch, _ = self._batch()
        self._tx(up, consumed_by_batch=batch)
        self.client.post(reverse("delete_upload", args=[up.pk]))
        self.assertTrue(Upload.objects.filter(pk=up.pk).exists())

    def test_upload_bersih_tetap_bisa_dihapus(self):
        up = self._upload()
        self._tx(up)
        self.client.post(reverse("delete_upload", args=[up.pk]))
        self.assertFalse(Upload.objects.filter(pk=up.pk).exists())

    def test_bulk_campuran_bersih_terhapus_terkunci_selamat(self):
        bersih = self._upload(name="bersih.xlsx")
        terkunci = self._upload(name="terkunci.xlsx")
        batch, run = self._batch()
        MatchResult.objects.create(run=run, bucket=MatchResult.Bucket.TIDAK,
                                   left=self._tx(terkunci), reason_code="no_money")
        r = self.client.post(reverse("bulk_delete_uploads"),
                             {"upload_ids": [bersih.pk, terkunci.pk]}, follow=True)
        self.assertFalse(Upload.objects.filter(pk=bersih.pk).exists())
        self.assertTrue(Upload.objects.filter(pk=terkunci.pk).exists())
        self.assertContains(r, "terkunci.xlsx")

    def test_tombol_hapus_terkunci_di_riwayat(self):
        up = self._upload()
        batch, run = self._batch()
        MatchResult.objects.create(run=run, bucket=MatchResult.Bucket.TIDAK,
                                   left=self._tx(up), reason_code="no_money")
        r = self.client.get(reverse("upload"))
        self.assertContains(r, "Dipakai hasil rekonsiliasi")

    def test_riwayat_punya_pilih_semua_dan_hapus_massal(self):
        self._upload(name="a.xlsx")
        r = self.client.get(reverse("upload"))
        self.assertContains(r, 'id="chkAll"')          # checkbox pilih semua
        self.assertContains(r, 'name="upload_ids"')    # checkbox per baris (tak terkunci)
        self.assertContains(r, "Hapus terpilih")       # tombol hapus massal


class DeleteUploadCliRunGuardTests(_Base):
    """W3-4a: run CLI (tanpa batch) juga mengunci upload — _locking_batches
    lama meng-exclude run tanpa batch, sehingga upload yang direferensi hasil
    run CLI bisa dihapus dan MatchResult-nya lenyap."""

    def _cli_run(self):
        return MatchRun.objects.create(
            relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol, batch=None,
        )

    def test_blokir_bila_direferensi_run_cli(self):
        up = self._upload(name="cli.xlsx")
        MatchResult.objects.create(run=self._cli_run(), bucket=MatchResult.Bucket.TIDAK,
                                   left=self._tx(up), reason_code="no_money")
        r = self.client.post(reverse("delete_upload", args=[up.pk]), follow=True)
        self.assertTrue(Upload.objects.filter(pk=up.pk).exists())
        self.assertContains(r, "run CLI")

    def test_bulk_melewati_upload_terkunci_run_cli(self):
        up = self._upload(name="cli-bulk.xlsx")
        MatchResult.objects.create(run=self._cli_run(), bucket=MatchResult.Bucket.COCOK,
                                   right=self._tx(up), reason_code="")
        r = self.client.post(reverse("bulk_delete_uploads"),
                             {"upload_ids": [up.pk]}, follow=True)
        self.assertTrue(Upload.objects.filter(pk=up.pk).exists())
        self.assertContains(r, "cli-bulk.xlsx")


class DeleteUploadDuplicateGuardTests(_Base):
    """W3-4b: baris upload A juga 'milik' upload B via M2M duplicate_transactions
    (file bank rolling/tumpang-tindih). Hapus A meng-cascade transaksinya →
    isi file B bolong senyap. Blokir A; B (pemegang link duplikat) tetap bebas."""

    def _pasangan_duplikat(self):
        a = self._upload(name="asli.xlsx")
        tx = self._tx(a)
        b = self._upload(name="pemegang-duplikat.xlsx")
        b.duplicate_transactions.add(tx)
        return a, b, tx

    def test_blokir_pemilik_baris_duplikat(self):
        a, b, tx = self._pasangan_duplikat()
        r = self.client.post(reverse("delete_upload", args=[a.pk]), follow=True)
        self.assertTrue(Upload.objects.filter(pk=a.pk).exists())
        self.assertContains(r, "duplikat")

    def test_pemegang_duplikat_tetap_bisa_dihapus(self):
        a, b, tx = self._pasangan_duplikat()
        self.client.post(reverse("delete_upload", args=[b.pk]))
        self.assertFalse(Upload.objects.filter(pk=b.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=tx.pk).exists(),
                        "transaksi milik upload asli tidak boleh ikut hilang")

    def test_bulk_melewati_pemilik_baris_duplikat(self):
        a, b, tx = self._pasangan_duplikat()
        r = self.client.post(reverse("bulk_delete_uploads"),
                             {"upload_ids": [a.pk]}, follow=True)
        self.assertTrue(Upload.objects.filter(pk=a.pk).exists())
        self.assertContains(r, "asli.xlsx")


class HollowDetectionTests(_Base):
    def test_batch_cangkang_dapat_banner(self):
        batch, _ = self._batch(summary={"buckets": {"cocok": 100, "perlu_tinjau": 5, "tidak_cocok": 10}})
        r = self.client.get(reverse("batch_detail", args=[batch.pk]))
        self.assertContains(r, BANNER)

    def test_batch_sehat_tanpa_banner(self):
        up = self._upload()
        batch, run = self._batch(summary={"buckets": {"cocok": 1, "perlu_tinjau": 0, "tidak_cocok": 0}})
        MatchResult.objects.create(run=run, bucket=MatchResult.Bucket.COCOK,
                                   left=self._tx(up), reason_code="")
        r = self.client.get(reverse("batch_detail", args=[batch.pk]))
        self.assertNotContains(r, BANNER)

    def test_run_cangkang_dapat_banner(self):
        batch, run = self._batch()
        run.summary = {"cocok": 6839, "perlu_tinjau": 310, "tidak_cocok": 709}
        run.save(update_fields=["summary"])
        r = self.client.get(reverse("run_detail", args=[run.pk]))
        self.assertContains(r, BANNER)

    def test_run_sehat_tanpa_banner(self):
        up = self._upload()
        batch, run = self._batch()
        run.summary = {"cocok": 1, "perlu_tinjau": 0, "tidak_cocok": 0}
        run.save(update_fields=["summary"])
        MatchResult.objects.create(run=run, bucket=MatchResult.Bucket.COCOK,
                                   left=self._tx(up), reason_code="")
        r = self.client.get(reverse("run_detail", args=[run.pk]))
        self.assertNotContains(r, BANNER)
