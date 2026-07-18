"""Hash BCA PDF harus stabil lintas export tumpang-tindih.

Bug: row_hash memuat `idx` posisi GLOBAL file — export periode overlap
(1-15 vs 1-31) menggeser posisi baris yang sama sehingga hash-nya berubah
→ transaksi lama masuk dobel. Fix meniru pola bni_pdf: occurrence-counter
per kunci stabil (tanggal, nominal, CR/DB, desc[:40]) dihitung urut dalam
file, bukan posisi baris global.
"""
from unittest.mock import patch

from django.test import SimpleTestCase

from sources.parsers.bca_pdf import BCAPDFParser


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePDF:
    def __init__(self, text):
        self.pages = [_FakePage(text)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _parse(lines):
    with patch("sources.parsers.bca_pdf.pdfplumber.open",
               return_value=_FakePDF("\n".join(lines))):
        return BCAPDFParser().parse("/fake.pdf")


T_AWAL = "30/06/2026 SETORAN TUNAI 250,000.00 CR"
T1 = "01/07/2026 TRSF E-BANKING CR 100,000.00BUDI SANTOSO 100,000.00 CR"
T2 = "02/07/2026 TRSF E-BANKING DB 50,000.00SITI AMINAH 50,000.00 DB"


class BCAPDFHashOverlapTests(SimpleTestCase):
    def test_export_overlap_baris_sama_hash_sama(self):
        # File pendek (1-15) vs file panjang (1-31 dgn baris ekstra di depan):
        # transaksi yang sama harus menghasilkan hash yang sama (dedup jalan).
        pendek = _parse([T1, T2])
        panjang = _parse([T_AWAL, T1, T2])
        self.assertEqual(len(pendek), 2)
        self.assertEqual(len(panjang), 3)
        self.assertEqual(pendek[0]["row_hash"], panjang[1]["row_hash"])
        self.assertEqual(pendek[1]["row_hash"], panjang[2]["row_hash"])

    def test_transaksi_identik_dalam_satu_file_hash_beda(self):
        # Dua transaksi kembar betulan dalam SATU file tetap dua baris berbeda.
        rows = _parse([T1, T1])
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["row_hash"], rows[1]["row_hash"])

    def test_transaksi_kembar_stabil_lintas_export(self):
        # Kembar ke-1/ke-2 harus dapat urutan kemunculan yang sama di export
        # overlap: hash pasangannya identik satu-satu.
        pendek = _parse([T1, T1])
        panjang = _parse([T_AWAL, T1, T1])
        self.assertEqual(pendek[0]["row_hash"], panjang[1]["row_hash"])
        self.assertEqual(pendek[1]["row_hash"], panjang[2]["row_hash"])
