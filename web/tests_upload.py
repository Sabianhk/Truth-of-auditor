from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from sources import services
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction


class UploadAnalyzeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")

    def test_analyze_detects_bri(self):
        f = SimpleUploadedFile(
            "bri.csv",
            b"TGL_TRAN,MUTASI_DEBET,MUTASI_KREDIT,DESK_TRAN\n",
            content_type="text/csv",
        )
        r = self.client.post(reverse("upload"), {"action": "analyze", "files": [f]})
        self.assertEqual(r.status_code, 200)
        preview = r.context["preview"]
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["parser_key"], "bri")
        self.assertFalse(preview[0]["needs_confirm"])


_ROW = {
    "occurred_at": datetime(2026, 6, 27, 10, 0), "posted_date": None, "jenis": "depo",
    "amount": Decimal("50000"), "credit_delta": Decimal("-50000"), "money_delta": Decimal("50000"),
    "fee": Decimal("0"), "bonus": Decimal("0"), "balance_after": None, "ticket_no": "D1",
    "username": "budi", "reference": "", "counterparty": "", "description": "", "raw": {},
    "row_hash": "commit-row-1",
}


class _DummyBracket:
    source_key = "bracket"

    def parse(self, path, flow=""):
        return [dict(_ROW)]


class UploadCommitTests(TestCase):
    def setUp(self):
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        SourceType.objects.get_or_create(key="bracket", defaults={"name": "Bracket"})
        self.lbs = Toko.objects.get(key="lbs")
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})

    def _stage_in_session(self, staged):
        """Daftarkan path staging ke sesi seolah hasil analyze (kontrak W3-1)."""
        session = self.client.session
        session["staged_paths"] = session.get("staged_paths", []) + [staged]
        session.save()

    def test_commit_ingests_and_sets_toko(self):
        staged = default_storage.save("staging/x.csv", ContentFile(b"dummy"))
        self._stage_in_session(staged)
        with patch.dict(services.PARSERS, {"dummy": _DummyBracket}, clear=False):
            r = self.client.post(reverse("upload"), {
                "action": "commit", "staged": [staged],
                "parser_key": ["dummy"], "flow": [""], "provider": "Nexus",
            })
        self.assertEqual(r.status_code, 302)
        up = Upload.objects.latest("id")
        self.assertEqual(up.toko, self.lbs)
        self.assertEqual(up.provider, "Nexus")
        self.assertEqual(up.rows_parsed, 1)
        self.assertFalse(default_storage.exists(staged))

    def test_commit_rejects_non_staging_path(self):
        n_up = Upload.objects.count()
        n_tx = Transaction.objects.count()
        with patch.dict(services.PARSERS, {"dummy": _DummyBracket}, clear=False), \
                patch("web.views.ingest", side_effect=AssertionError("must not ingest")):
            r = self.client.post(reverse("upload"), {
                "action": "commit", "staged": ["uploads/x"],
                "parser_key": ["dummy"], "flow": [""], "provider": "Nexus",
            })
        self.assertEqual(r.status_code, 302)  # redirect, tidak crash
        self.assertEqual(Upload.objects.count(), n_up)  # tidak ada upload dibuat
        self.assertEqual(Transaction.objects.count(), n_tx)

    def test_commit_menolak_path_tanpa_analyze_di_sesi(self):
        """W3-1: path staging yang tak pernah dianalisa di sesi ini ditolak —
        file TIDAK terhapus (cegah user menghapus/commit staging user lain)."""
        staged = default_storage.save("staging/milik-lain.csv", ContentFile(b"dummy"))
        try:
            n_up = Upload.objects.count()
            with patch.dict(services.PARSERS, {"dummy": _DummyBracket}, clear=False), \
                    patch("web.views.ingest", side_effect=AssertionError("must not ingest")):
                r = self.client.post(reverse("upload"), {
                    "action": "commit", "staged": [staged],
                    "parser_key": ["dummy"], "flow": [""], "provider": "Nexus",
                })
            self.assertEqual(r.status_code, 302)
            self.assertEqual(Upload.objects.count(), n_up)
            self.assertTrue(default_storage.exists(staged),
                            "file staging milik sesi lain tidak boleh terhapus")
        finally:
            if default_storage.exists(staged):
                default_storage.delete(staged)

    def test_commit_membersihkan_path_dari_sesi(self):
        staged = default_storage.save("staging/x.csv", ContentFile(b"dummy"))
        self._stage_in_session(staged)
        with patch.dict(services.PARSERS, {"dummy": _DummyBracket}, clear=False):
            self.client.post(reverse("upload"), {
                "action": "commit", "staged": [staged],
                "parser_key": ["dummy"], "flow": [""], "provider": "Nexus",
            })
        self.assertNotIn(staged, self.client.session.get("staged_paths", []))

    def test_alur_analyze_lalu_commit_tetap_jalan(self):
        """Alur normal: analyze mendaftarkan path ke sesi → commit diterima."""
        f = SimpleUploadedFile(
            "bri.csv",
            b"TGL_TRAN,MUTASI_DEBET,MUTASI_KREDIT,DESK_TRAN\n",
            content_type="text/csv",
        )
        r = self.client.post(reverse("upload"), {"action": "analyze", "files": [f]})
        staged = r.context["preview"][0]["staged"]
        self.assertIn(staged, self.client.session.get("staged_paths", []))
        r2 = self.client.post(reverse("upload"), {
            "action": "commit", "staged": [staged],
            "parser_key": ["bri"], "flow": [""],
        })
        self.assertEqual(r2.status_code, 302)
        self.assertFalse(default_storage.exists(staged))
        self.assertNotIn(staged, self.client.session.get("staged_paths", []))

    def test_analyze_berulang_menambah_daftar_sesi(self):
        """Analyze bisa dipanggil berkali-kali — daftar path sesi akumulatif."""
        for nama in ("a.csv", "b.csv"):
            f = SimpleUploadedFile(
                nama,
                b"TGL_TRAN,MUTASI_DEBET,MUTASI_KREDIT,DESK_TRAN\n",
                content_type="text/csv",
            )
            self.client.post(reverse("upload"), {"action": "analyze", "files": [f]})
        paths = self.client.session.get("staged_paths", [])
        self.assertEqual(len(paths), 2)
        for p in paths:
            default_storage.delete(p)

    def test_commit_rejects_path_traversal(self):
        n_up = Upload.objects.count()
        with patch.dict(services.PARSERS, {"dummy": _DummyBracket}, clear=False), \
                patch("web.views.ingest", side_effect=AssertionError("must not ingest")):
            r = self.client.post(reverse("upload"), {
                "action": "commit", "staged": ["staging/../etc/passwd"],
                "parser_key": ["dummy"], "flow": [""], "provider": "Nexus",
            })
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Upload.objects.count(), n_up)


class UploadHistoryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.u = User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        self.lbs = Toko.objects.get(key="lbs")
        self.slo = Toko.objects.get(key="slo")
        self.bracket = SourceType.objects.get_or_create(key="bracket", defaults={"name": "Bracket"})[0]

    def test_history_scoped_to_active_toko(self):
        Upload.objects.create(source_type=self.bracket, toko=self.lbs, original_name="lbs-file.xlsx", uploaded_by=self.u)
        Upload.objects.create(source_type=self.bracket, toko=self.slo, original_name="slo-file.xlsx", uploaded_by=self.u)
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})
        r = self.client.get(reverse("upload"))
        self.assertContains(r, "lbs-file.xlsx")
        self.assertNotContains(r, "slo-file.xlsx")


class UploadLockedAnnotationTests(TestCase):
    """Anotasi `locked` di _uploads_for: upload terkunci bila buktinya dipakai
    hasil rekon (MatchResult left/right) ATAU transaksinya dikonsumsi batch.
    Karakterisasi semantik — penjaga saat query-nya dioptimalkan (split Exists;
    bentuk OR-dalam-satu-subquery = seq-scan MatchResult per baris di Postgres,
    terukur 10,8 dtk utk 20 upload di prod)."""

    def setUp(self):
        from reconciliation.models import ToleranceProfile

        User = get_user_model()
        self.u = User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.toko = Toko.objects.get(key="lbs")
        self.st = SourceType.objects.get_or_create(key="bracket", defaults={"name": "Bracket"})[0]
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self._n = 0

    def _upload_with_tx(self):
        self._n += 1
        up = Upload.objects.create(source_type=self.st, toko=self.toko,
                                   original_name=f"f{self._n}.xlsx", uploaded_by=self.u)
        tx = Transaction.objects.create(
            upload=up, source_type=self.st, toko=self.toko, jenis="depo",
            amount=Decimal("50000"), money_delta=Decimal("50000"),
            occurred_at=datetime(2026, 6, 27, 10, 0), row_hash=f"lk{self._n}",
        )
        return up, tx

    def _locked_map(self):
        from web.views import _uploads_for

        return {u.original_name: u.locked for u in _uploads_for(self.toko)}

    def test_locked_bila_left_right_consumed_dan_bebas(self):
        from reconciliation.models import MatchResult, MatchRun, ReconBatch

        run = MatchRun.objects.create(relation=MatchRun.Relation.PANEL_BANK, tolerance=self.tol)
        up_left, tx_left = self._upload_with_tx()
        MatchResult.objects.create(run=run, bucket=MatchResult.Bucket.TIDAK,
                                   reason_code="no_money", left=tx_left)
        up_right, tx_right = self._upload_with_tx()
        MatchResult.objects.create(run=run, bucket=MatchResult.Bucket.COCOK, right=tx_right)
        up_cons, tx_cons = self._upload_with_tx()
        batch = ReconBatch.objects.create(toko=self.toko, tolerance=self.tol)
        tx_cons.consumed_by_batch = batch
        tx_cons.save(update_fields=["consumed_by_batch"])
        up_free, _ = self._upload_with_tx()

        locked = self._locked_map()
        self.assertTrue(locked[up_left.original_name], "referensi left harus mengunci")
        self.assertTrue(locked[up_right.original_name], "referensi right harus mengunci")
        self.assertTrue(locked[up_cons.original_name], "konsumsi batch harus mengunci")
        self.assertFalse(locked[up_free.original_name], "upload bebas tidak terkunci")


class UploadHistoryPaginationTests(TestCase):
    """Riwayat Upload berhalaman (pager seragam spt halaman lain) — end user
    perlu menghapus file tanggal lama yang dulu tersembunyi di luar 20 terakhir."""

    def setUp(self):
        self.u = User_ = get_user_model().objects.create_user(
            "adm", password="pw12345", role="admin")
        self.client.login(username="adm", password="pw12345")
        self.lbs = Toko.objects.get(key="lbs")
        st = SourceType.objects.get_or_create(key="bracket", defaults={"name": "Bracket"})[0]
        for i in range(25):  # tertua = riwayat-00 (id terkecil, halaman terakhir)
            Upload.objects.create(source_type=st, toko=self.lbs,
                                  original_name=f"riwayat-{i:02d}.xlsx", uploaded_by=self.u)
        self.client.post(reverse("set_toko"), {"toko_id": self.lbs.id})

    def test_riwayat_berhalaman_20_per_halaman(self):
        r = self.client.get(reverse("upload"))
        self.assertEqual(len(r.context["uploads"]), 20)          # hal 1 = 20 terbaru
        self.assertContains(r, "riwayat-24.xlsx")
        self.assertNotContains(r, "riwayat-00.xlsx")             # tertua tak di hal 1
        self.assertContains(r, "?page=2")                        # pager tampil
        r2 = self.client.get(reverse("upload"), {"page": "2"})
        self.assertContains(r2, "riwayat-00.xlsx")               # tertua kini terjangkau
        self.assertEqual(len(r2.context["uploads"]), 5)

    def test_jumlah_total_file_ditampilkan(self):
        r = self.client.get(reverse("upload"))
        self.assertContains(r, "25 file")                        # total, bukan "20 file terakhir"

    def test_bulk_delete_kembali_ke_halaman_asal(self):
        target = Upload.objects.filter(toko=self.lbs).order_by("id").first()
        r = self.client.post(reverse("bulk_delete_uploads"),
                             {"upload_ids": [target.id], "page": "2"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], reverse("upload") + "?page=2")
        self.assertFalse(Upload.objects.filter(pk=target.pk).exists())
