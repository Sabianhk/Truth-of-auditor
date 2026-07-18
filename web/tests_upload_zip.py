"""Upload zip & folder: arsip .zip diekstrak server-side jadi baris preview per-isi
(flow analyze→commit tak berubah); file junk (__MACOSX, dotfile, ~$, ekstensi di
luar xlsx/xls/csv/pdf) dilewati dengan hitungan; guard zip-bomb (jumlah & ukuran);
input folder (webkitdirectory) tersedia di form.
"""
import io
import zipfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

BRI_HEADER = b"TGL_TRAN,MUTASI_DEBET,MUTASI_KREDIT,DESK_TRAN\n"


def _zip_upload(entries, name="sample.zip"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, data in entries:
            zf.writestr(n, data)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="application/zip")


class _Base(TestCase):
    def setUp(self):
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")

    def _analyze(self, files):
        return self.client.post(reverse("upload"), {"action": "analyze", "files": files})


class ZipUploadTests(_Base):
    def test_zip_diekstrak_jadi_preview_per_isi(self):
        f = _zip_upload([
            ("27-06/DP/bri_dp.csv", BRI_HEADER),
            ("27-06/WD/bri_wd.csv", BRI_HEADER),
        ])
        r = self._analyze([f])
        self.assertEqual(r.status_code, 200)
        preview = r.context["preview"]
        self.assertEqual(len(preview), 2)
        self.assertEqual({p["parser_key"] for p in preview}, {"bri"})
        self.assertEqual(sorted(p["name"] for p in preview), ["bri_dp.csv", "bri_wd.csv"])

    def test_zip_junk_dilewati(self):
        f = _zip_upload([
            ("__MACOSX/._bri.csv", b"junk"),
            ("27-06/.DS_Store", b"junk"),
            ("27-06/catatan.txt", b"junk"),
            ("27-06/~$temp.xlsx", b"junk"),
            ("27-06/ok.csv", BRI_HEADER),
        ])
        r = self._analyze([f])
        preview = r.context["preview"]
        self.assertEqual([p["name"] for p in preview], ["ok.csv"])
        self.assertContains(r, "dilewati")

    def test_zip_kebanyakan_file_ditolak(self):
        f = _zip_upload([(f"f{i}.csv", BRI_HEADER) for i in range(201)])
        r = self._analyze([f])
        self.assertEqual(r.context["preview"], [])
        self.assertContains(r, "terlalu banyak")

    def test_zip_rusak_error_jelas(self):
        f = SimpleUploadedFile("rusak.zip", b"bukan isi zip", content_type="application/zip")
        r = self._analyze([f])
        self.assertEqual(r.context["preview"], [])
        self.assertContains(r, "rusak.zip")

    def test_zip_member_rusak_pesan_jelas_bukan_500(self):
        """W3-6b: CRC member korup → BadZipFile saat zf.read dulu meledak 500;
        kini pesan "zip rusak"."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("rusak.csv", BRI_HEADER)
        raw = bytearray(buf.getvalue())
        # byte pertama isi member: header lokal 30 byte + panjang nama file
        raw[30 + len("rusak.csv")] ^= 0xFF
        f = SimpleUploadedFile("arsip.zip", bytes(raw), content_type="application/zip")
        r = self._analyze([f])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["preview"], [])
        self.assertContains(r, "zip rusak")

    def test_zip_member_kebesaran_dilewati_dan_dilaporkan(self):
        """W3-6d: cap per-member 50MB konsisten dgn cap file langsung."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("besar.csv", BRI_HEADER * 1000)   # 44.000 byte
            zf.writestr("kecil.csv", BRI_HEADER)
        f = SimpleUploadedFile("arsip.zip", buf.getvalue(), content_type="application/zip")
        with patch("web.views._FILE_MAX_BYTES", 2000):
            r = self._analyze([f])
        preview = r.context["preview"]
        self.assertEqual([p["name"] for p in preview], ["kecil.csv"])
        self.assertContains(r, "besar.csv")
        self.assertContains(r, "melebihi")

    def test_xlsx_tidak_dianggap_zip(self):
        # xlsx = arsip zip juga (magic PK) — keputusan pakai EKSTENSI: .xlsx tetap
        # satu baris preview, tidak diekstrak.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("xl/workbook.xml", b"<x/>")
        f = SimpleUploadedFile("data.xlsx", buf.getvalue())
        r = self._analyze([f])
        preview = r.context["preview"]
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["name"], "data.xlsx")

    def test_junk_langsung_juga_dilewati(self):
        files = [
            SimpleUploadedFile(".DS_Store", b"junk"),
            SimpleUploadedFile("bri.csv", BRI_HEADER, content_type="text/csv"),
        ]
        r = self._analyze(files)
        preview = r.context["preview"]
        self.assertEqual([p["name"] for p in preview], ["bri.csv"])


class UploadSettingsTests(TestCase):
    def test_cap_jumlah_file_per_request(self):
        """W3-6e: folder picker bisa >100 file — default Django (100) membuat
        request 400 sebelum view jalan."""
        from django.conf import settings

        self.assertEqual(settings.DATA_UPLOAD_MAX_NUMBER_FILES, 250)


class FolderInputTests(_Base):
    def test_form_punya_input_folder_dan_terima_zip(self):
        r = self.client.get(reverse("upload"))
        self.assertContains(r, "webkitdirectory")
        self.assertContains(r, ".zip")


class DragDropMarkupTests(_Base):
    """Dropzone interaktif: drag-drop file & folder (webkitGetAsEntry traversal),
    pilihan terakumulasi (pilih dua kali = gabung, bukan reset), tombol bersihkan."""

    def test_dropzone_punya_handler_drop_dan_traversal_folder(self):
        r = self.client.get(reverse("upload"))
        self.assertContains(r, 'id="dropzone"')
        self.assertContains(r, "webkitGetAsEntry")

    def test_ada_tombol_bersihkan_pilihan(self):
        r = self.client.get(reverse("upload"))
        self.assertContains(r, 'id="fclear"')
