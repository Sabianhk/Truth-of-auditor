"""Hash bracket baris manual ledger (tanpa Transaction ID & ticket).

Bug: row_hash bracket = [Transaction ID, ticket_no, username, amount] — baris
manual ("Beban Admin QRIS", Hutang, Piutang, dst) tanpa ID & ticket dengan
nominal sama lintas riwayat menghasilkan hash identik → baris kedua dibuang
senyap saat ingest. Baris manual kini diberi pembeda Tanggal+Jam+Description;
baris ber-ID/ticket TETAP memakai formula lama (idempotensi data lama terjaga).
"""
import os
import tempfile

from django.test import SimpleTestCase
from openpyxl import Workbook

from sources.parsers.bracket import BracketParser
from sources.parsers.base import row_hash

HEADER = [
    "Tanggal", "Jam", "Asset Bank", "ID", "Description", "Member", "Username",
    "Product", "Expense", "No. Rek Bank Member", "Bank", "Total", "Saldo Akhir",
    "Credit Awal", "Credit Akhir", "Kategori", "Status", "OP", "Transaction ID",
    "Transaction Date", "Status Backdated",
]


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    fd, p = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(p)
    return p


def _manual(tanggal, jam, desc, total="-5000"):
    """Baris manual ledger: tanpa Transaction ID dan tanpa ticket di Description."""
    r = [""] * len(HEADER)
    r[0], r[1], r[4], r[11], r[15] = tanggal, jam, desc, total, "Beban Admin QRIS"
    return r


def _bertiket(tanggal, desc, username, total, tx_id):
    r = [""] * len(HEADER)
    r[0], r[4], r[6], r[11], r[15], r[18] = tanggal, desc, username, total, "Deposit", tx_id
    return r


def _parse(rows):
    path = _xlsx(rows)
    try:
        return BracketParser().parse(path)
    finally:
        os.remove(path)


class BracketManualRowHashTests(SimpleTestCase):
    def test_manual_nominal_sama_tanggal_beda_hash_beda(self):
        rows = _parse([
            _manual("01/07/2026", "10:00", "Beban Admin QRIS"),
            _manual("02/07/2026", "10:00", "Beban Admin QRIS"),
        ])
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["row_hash"], rows[1]["row_hash"])

    def test_manual_deskripsi_beda_hash_beda(self):
        rows = _parse([
            _manual("01/07/2026", "10:00", "Hutang"),
            _manual("01/07/2026", "10:00", "Piutang"),
        ])
        self.assertNotEqual(rows[0]["row_hash"], rows[1]["row_hash"])

    def test_manual_identik_betulan_hash_sama(self):
        rows = _parse([
            _manual("01/07/2026", "10:00", "Beban Admin QRIS"),
            _manual("01/07/2026", "10:00", "Beban Admin QRIS"),
        ])
        self.assertEqual(rows[0]["row_hash"], rows[1]["row_hash"])

    def test_baris_ber_transaction_id_tetap_formula_lama(self):
        # Idempotensi data lama: hash baris ber-ID TIDAK boleh berubah.
        rows = _parse([
            _bertiket("01/07/2026", "Direct Deposit - D1757153", "budi", "50000", "TX-123"),
        ])
        r = rows[0]
        self.assertEqual(
            r["row_hash"],
            row_hash("bracket", ["TX-123", "D1757153", "budi", r["amount"]]),
        )

    def test_baris_ber_ticket_hash_tak_tergantung_tanggal(self):
        # Baris ber-ticket (walau Transaction ID kosong) tetap formula lama:
        # Tanggal TIDAK ikut hash — dua export yang menampilkan transaksi sama
        # harus tetap terdedup.
        rows = _parse([
            _bertiket("01/07/2026", "Direct Withdraw - W1757092", "siti", "-75000", ""),
            _bertiket("02/07/2026", "Direct Withdraw - W1757092", "siti", "-75000", ""),
        ])
        self.assertEqual(rows[0]["row_hash"], rows[1]["row_hash"])
