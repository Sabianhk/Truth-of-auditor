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
