"""Test deteksi & guard file terenkripsi (Mandiri e-statement) di jalur ingest,
plus parsing sel tanggal bertipe datetime."""
import os
import tempfile
from datetime import date, datetime
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook

from sources import services
from sources.parsers.banks import MandiriParser

# OLE2/CDFV2 compound-file header — penanda xlsx terenkripsi.
_OLE2_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
_ZIP_MAGIC = b"PK\x03\x04"


class _DummyParser:
    """Parser dummy — dipakai untuk memastikan guard password menyala SEBELUM parse."""

    source_key = "bracket"

    def parse(self, path, flow=""):  # pragma: no cover - tak boleh terpanggil di test guard
        raise AssertionError("parse() tidak boleh dipanggil untuk file terenkripsi tanpa password")


class IsEncryptedXlsxTests(TestCase):
    def test_ole2_magic_is_encrypted(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(_OLE2_MAGIC + b"\x00" * 512)
            path = f.name
        self.assertTrue(services.is_encrypted_xlsx(path))

    def test_zip_magic_is_not_encrypted(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(_ZIP_MAGIC + b"\x00" * 512)
            path = f.name
        self.assertFalse(services.is_encrypted_xlsx(path))

    def test_missing_file_returns_false(self):
        self.assertFalse(services.is_encrypted_xlsx("/no/such/file.xlsx"))


class _FakeOle:
    """Tiruan olefile.OleFileIO: hanya `exists()` atas daftar stream tertentu."""

    def __init__(self, streams):
        self._streams = set(streams)

    def exists(self, name):
        return name in self._streams

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _ole2_file():
    with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as f:
        f.write(_OLE2_MAGIC + b"\x00" * 512)
        return f.name


class BiffVsEncryptedTests(TestCase):
    """Magic OLE2 ≠ pasti terenkripsi: .xls BIFF lawas juga OLE2.

    Bug: semua OLE2 diminta password lalu buntu \"Password salah\". Kini stream
    dicek via olefile: EncryptionInfo/EncryptedPackage = terenkripsi;
    Workbook/Book tanpa itu = BIFF lawas -> error jelas dari ingest.
    """

    def test_biff_bukan_terenkripsi(self):
        path = _ole2_file()
        with patch("olefile.OleFileIO", return_value=_FakeOle({"Workbook"})):
            self.assertFalse(services.is_encrypted_xlsx(path))

    def test_biff_ingest_error_save_as_xlsx(self):
        path = _ole2_file()
        with patch("olefile.OleFileIO", return_value=_FakeOle({"Book"})):
            with self.assertRaises(ValueError) as ctx:
                services.ingest("bracket", path)
        self.assertIn("Save As .xlsx", str(ctx.exception))

    def test_stream_enkripsi_tetap_terenkripsi(self):
        path = _ole2_file()
        with patch("olefile.OleFileIO",
                   return_value=_FakeOle({"EncryptionInfo", "EncryptedPackage"})):
            self.assertTrue(services.is_encrypted_xlsx(path))

    def test_ole2_tak_terbaca_konservatif_dianggap_terenkripsi(self):
        # olefile gagal parse (isi sampah) -> pertahankan perilaku lama:
        # minta password (lebih aman daripada crash parser downstream).
        path = _ole2_file()
        self.assertTrue(services.is_encrypted_xlsx(path))


class IngestEncryptedGuardTests(TestCase):
    def test_missing_password_raises_before_parse(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(_OLE2_MAGIC + b"\x00" * 512)
            path = f.name
        with patch.dict(services.PARSERS, {"dummy": _DummyParser}, clear=False):
            with self.assertRaises(ValueError) as cm:
                services.ingest("dummy", path, password="")
        self.assertIn("password", str(cm.exception).lower())


def _mandiri_xlsx_typed(tanggal, baris_jam=None):
    """e-Statement minimal dgn sel Tanggal BERTIPE (datetime/date), bukan string."""
    wb = Workbook()
    ws = wb.active
    ws.append(["No", "Tanggal", "Keterangan", "Dana Masuk (IDR)",
               "Dana Keluar (IDR)", "Saldo (IDR)"])
    ws.append([1, tanggal, "Transfer ke BANK MANDIRI BUDI", "", "100.000,00",
               "2.004.500,00"])
    if baris_jam is not None:
        ws.append(["", baris_jam, "lanjutan keterangan", "", "", ""])
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    return path


class MandiriTypedDateTests(SimpleTestCase):
    """Bug: openpyxl bisa memberi sel Tanggal sbg datetime object — str(datetime)
    + " " + jam gagal diparse -> occurred_at None tapi baris tetap tersimpan
    (lolos dari window matching secara senyap)."""

    def _parse(self, tanggal, baris_jam=None):
        path = _mandiri_xlsx_typed(tanggal, baris_jam)
        try:
            return MandiriParser().parse(path)
        finally:
            os.remove(path)

    def test_tanggal_datetime_dengan_baris_jam(self):
        rows = self._parse(datetime(2026, 6, 27), "10:11 WIB")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurred_at"], datetime(2026, 6, 27, 10, 11))
        self.assertEqual(rows[0]["posted_date"], date(2026, 6, 27))

    def test_tanggal_datetime_tanpa_baris_jam(self):
        rows = self._parse(datetime(2026, 6, 27, 8, 30))
        self.assertEqual(rows[0]["occurred_at"], datetime(2026, 6, 27, 8, 30))

    def test_tanggal_date_polos(self):
        rows = self._parse(date(2026, 6, 27))
        self.assertEqual(rows[0]["occurred_at"], datetime(2026, 6, 27, 0, 0))

    def test_tanggal_string_tetap_jalan(self):
        rows = self._parse("27 Jun 2026", "10:11 WIB")
        self.assertEqual(rows[0]["occurred_at"], datetime(2026, 6, 27, 10, 11))
