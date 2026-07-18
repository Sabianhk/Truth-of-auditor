"""Guard parser gateway: deteksi footer NXPay & kewajiban flow parser dua-arah."""
import os
import tempfile

from django.test import SimpleTestCase
from openpyxl import Workbook

from sources.parsers.gateways import NXPayParser

NX_HEADER = ["Ticket Number", "Username", "Amount", "Date", "Admin Fee",
             "Account Title", "Payment Type", "Status"]


def _nx_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(["NXPAY REPORT"])  # baris 1 = judul report
    ws.append(NX_HEADER)
    for r in rows:
        ws.append(r)
    fd, p = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(p)
    return p


def _parse_nx(rows, flow="dp"):
    path = _nx_xlsx(rows)
    try:
        return NXPayParser().parse(path, flow=flow)
    finally:
        os.remove(path)


class NXPayFooterTests(SimpleTestCase):
    def test_username_mengandung_total_tetap_terparse(self):
        # Bug: substring "total" di username -> player "totalwin88" dibuang senyap.
        rows = _parse_nx([
            ["D1761515", "totalwin88", "50000", "7/12/2026 10:00:00 AM", "0", "TOTALWIN", "QRIS", "SETTLED"],
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "totalwin88")

    def test_footer_tanpa_ticket_tetap_dilewati(self):
        rows = _parse_nx([
            ["D1761515", "budi", "50000", "7/12/2026 10:00:00 AM", "0", "BUDI", "QRIS", "SETTLED"],
            ["", "Grand Total", "50000", "", "", "", "", ""],
            ["", "Total", "50000", "", "", "", "", ""],
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "budi")

    def test_footer_total_dengan_ticket_kolom_bergeser_tetap_dilewati(self):
        # Jaga-jaga eksporter aneh: baris footer berlabel "Total ..." di kolom
        # Username tetap dianggap footer (equality/prefix, bukan substring).
        rows = _parse_nx([
            ["x", "Total 25 transaksi", "50000", "", "", "", "", ""],
        ])
        self.assertEqual(rows, [])


class GatewayFlowWajibTests(SimpleTestCase):
    """Parser gateway dua-arah (baca `flow`) wajib tahu arah file.

    Bug: flow selain "wd" diam-diam dianggap DP — file WD tanpa token "wd" di
    nama = seluruh file kebalik tanda. Kini fail-loud: ValueError yang tampil
    sebagai error file saat commit upload.
    """

    def test_nxpay_flow_kosong_raise(self):
        from sources.parsers.gateways import NXPayParser
        with self.assertRaises(ValueError) as ctx:
            NXPayParser().parse("/tidak/dipakai.xlsx", flow="")
        self.assertIn("DP/WD", str(ctx.exception))

    def test_qrflyer_flow_kosong_raise(self):
        from sources.parsers.gateways import QRFlyerParser
        with self.assertRaises(ValueError):
            QRFlyerParser().parse("/tidak/dipakai.xlsx", flow="")

    def test_qhoki_flow_asing_raise(self):
        from sources.parsers.gateways import QHokiParser
        with self.assertRaises(ValueError):
            QHokiParser().parse("/tidak/dipakai.csv", flow="both")

    def test_nxpay_flow_wd_membalik_tanda(self):
        rows = _parse_nx([
            ["W1761515", "budi", "50000", "7/12/2026 10:00:00 AM", "0", "BUDI", "QRIS", "SETTLED"],
        ], flow="wd")
        self.assertEqual(rows[0]["jenis"], "wd")
        self.assertLess(rows[0]["money_delta"], 0)


class DetectFlowTests(SimpleTestCase):
    """detect_flow: token utuh (dipisah non-alfanumerik), bukan substring."""

    def _flow(self, name):
        from sources.management.commands.ingest import detect_flow
        return detect_flow(name)

    def test_crowd_bukan_wd(self):
        self.assertEqual(self._flow("MUTASI CROWDFUND.xlsx"), "")

    def test_wd_spasi(self):
        self.assertEqual(self._flow("WD NXPAY 12-07.xlsx"), "wd")

    def test_wd_underscore(self):
        self.assertEqual(self._flow("27_JUNI_2026_WD_BRI_PANCA_SENTANA.csv"), "wd")

    def test_dp_terdeteksi(self):
        self.assertEqual(self._flow("HISTORI DP PANEL OKE25 27-06.xlsx"), "dp")

    def test_updates_bukan_dp(self):
        self.assertEqual(self._flow("updates-juli.xlsx"), "")

    def test_tanpa_token(self):
        self.assertEqual(self._flow("MUTASI REKENING JULI.xlsx"), "")
