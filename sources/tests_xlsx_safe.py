import io, os, tempfile, zipfile
from datetime import datetime
from django.test import SimpleTestCase
from openpyxl import Workbook
from sources.parsers.base import read_xlsx_rows, _raw_xlsx_rows

_CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>'
_RELS = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
_WB = '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
_WBR = '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
# Sheet TANPA <dimension> (mereplikasi exporter COR): inline strings.
_SHEET = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
          '<row r="1"><c r="A1" t="inlineStr"><is><t>Transaction ID</t></is></c><c r="B1" t="inlineStr"><is><t>Amount</t></is></c></row>'
          '<row r="2"><c r="A2" t="inlineStr"><is><t>abc-123</t></is></c><c r="B2" t="inlineStr"><is><t>50000</t></is></c></row>'
          '</sheetData></worksheet>')

def _make_nodim_xlsx():
    fd, path = tempfile.mkstemp(suffix=".xlsx"); os.close(fd)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _WB)
        z.writestr("xl/_rels/workbook.xml.rels", _WBR)
        z.writestr("xl/worksheets/sheet1.xml", _SHEET)
    return path

class XlsxSafeTests(SimpleTestCase):
    def test_raw_reader_membaca_inline_strings(self):
        path = _make_nodim_xlsx()
        try:
            rows = _raw_xlsx_rows(path)
        finally:
            os.remove(path)
        self.assertEqual(rows[0][:2], ["Transaction ID", "Amount"])
        self.assertEqual(rows[1][:2], ["abc-123", "50000"])

    def test_raw_reader_nrows_early_stop(self):
        path = _make_nodim_xlsx()
        try:
            rows = _raw_xlsx_rows(path, nrows=1)
        finally:
            os.remove(path)
        self.assertEqual(len(rows), 1)

    def test_read_xlsx_rows_tahan_tanpa_dimension(self):
        path = _make_nodim_xlsx()
        try:
            headers, dicts = read_xlsx_rows(path, header_row=1)
        finally:
            os.remove(path)
        self.assertEqual(len(dicts), 1)
        self.assertEqual(dicts[0]["Transaction ID"], "abc-123")
        self.assertEqual(str(dicts[0]["Amount"]), "50000")

    def test_sel_tanpa_atribut_r_pakai_posisi_berjalan(self):
        # Exporter minimal boleh menghilangkan atribut r pada <c> — dulu semua
        # jatuh ke idx 0 dan saling timpa (hanya sel terakhir yang selamat).
        sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                 '<row><c t="inlineStr"><is><t>a</t></is></c>'
                 '<c t="inlineStr"><is><t>b</t></is></c>'
                 '<c t="inlineStr"><is><t>c</t></is></c></row>'
                 '</sheetData></worksheet>')
        fd, path = tempfile.mkstemp(suffix=".xlsx"); os.close(fd)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("[Content_Types].xml", _CT)
            z.writestr("_rels/.rels", _RELS)
            z.writestr("xl/workbook.xml", _WB)
            z.writestr("xl/_rels/workbook.xml.rels", _WBR)
            z.writestr("xl/worksheets/sheet1.xml", sheet)
        try:
            rows = _raw_xlsx_rows(path)
        finally:
            os.remove(path)
        self.assertEqual(rows, [["a", "b", "c"]])

    def test_sel_campuran_ber_r_dan_tanpa_r(self):
        # Sel tanpa r melanjutkan dari posisi sel ber-r terakhir.
        sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                 '<row r="1"><c r="B1" t="inlineStr"><is><t>x</t></is></c>'
                 '<c t="inlineStr"><is><t>y</t></is></c></row>'
                 '</sheetData></worksheet>')
        fd, path = tempfile.mkstemp(suffix=".xlsx"); os.close(fd)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("[Content_Types].xml", _CT)
            z.writestr("_rels/.rels", _RELS)
            z.writestr("xl/workbook.xml", _WB)
            z.writestr("xl/_rels/workbook.xml.rels", _WBR)
            z.writestr("xl/worksheets/sheet1.xml", sheet)
        try:
            rows = _raw_xlsx_rows(path)
        finally:
            os.remove(path)
        self.assertEqual(rows, [["", "x", "y"]])

    def test_cap_dekompresi_zip_bomb(self):
        # xlsx kecil bisa mengembang GB saat dekompresi (zip-bomb). ZipInfo
        # dipalsukan besar via patch getinfo — ukuran diperiksa SEBELUM read.
        from unittest.mock import patch
        path = _make_nodim_xlsx()
        real_getinfo = zipfile.ZipFile.getinfo

        def _bengkak(self, name):
            info = real_getinfo(self, name)
            info.file_size = 200 * 1024 * 1024  # 200MB > cap 150MB
            return info

        try:
            with patch.object(zipfile.ZipFile, "getinfo", _bengkak):
                with self.assertRaises(ValueError) as ctx:
                    _raw_xlsx_rows(path)
        finally:
            os.remove(path)
        self.assertIn("terlalu besar", str(ctx.exception))

    def test_file_normal_di_bawah_cap_tetap_terbaca(self):
        path = _make_nodim_xlsx()
        try:
            rows = _raw_xlsx_rows(path)
        finally:
            os.remove(path)
        self.assertEqual(rows[0][:2], ["Transaction ID", "Amount"])

    def test_wellformed_data_mempertahankan_nilai_typed(self):
        # File well-formed dengan baris data TIDAK boleh jatuh ke raw reader (yang
        # mengembalikan string) — nilai typed (float/datetime) harus utuh.
        wb = Workbook(); ws = wb.active
        ws.append(["Amount", "Date"])
        ws.append([50000.5, datetime(2026, 7, 7, 10, 0)])
        fd, path = tempfile.mkstemp(suffix=".xlsx"); os.close(fd)
        wb.save(path)
        try:
            _, dicts = read_xlsx_rows(path, header_row=1)
        finally:
            os.remove(path)
        self.assertEqual(len(dicts), 1)
        self.assertEqual(dicts[0]["Amount"], 50000.5)
        self.assertEqual(dicts[0]["Date"], datetime(2026, 7, 7, 10, 0))
