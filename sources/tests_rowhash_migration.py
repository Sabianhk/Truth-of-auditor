"""W6-3: migrasi recompute row_hash — helper harus MENGHASILKAN hash yang sama
persis dengan parser aktual (anti-drift), dan penerapannya di tabel harus
meng-update baris ber-hash formula lama tanpa melanggar unique constraint
(collision → baris dibiarkan, tidak dihapus)."""
import csv
import os
import tempfile
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook

from sources.parsers.base import row_hash
from sources.parsers.bca_pdf import BCAPDFParser
from sources.parsers.bracket import BracketParser
from sources.parsers.gateways import RPayGatewayParser
from sources.rowhash_recompute import (
    is_bca_pdf_raw,
    is_rpay_dp_raw,
    recompute_all,
    recompute_bca_pdf_hashes,
    recompute_bracket_hash,
    recompute_rpay_dp_hash,
)
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction

BRACKET_HEADER = [
    "Tanggal", "Jam", "Asset Bank", "ID", "Description", "Member", "Username",
    "Product", "Expense", "No. Rek Bank Member", "Bank", "Total", "Saldo Akhir",
    "Credit Awal", "Credit Akhir", "Kategori", "Status", "OP", "Transaction ID",
    "Transaction Date", "Status Backdated",
]


def _bracket_rows(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(BRACKET_HEADER)
    for r in rows:
        ws.append(r)
    fd, p = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(p)
    try:
        return BracketParser().parse(p)
    finally:
        os.remove(p)


class BracketRecomputeTests(SimpleTestCase):
    """Anti-drift: hash dari recompute(raw) == hash parser utk baris yang sama."""

    def test_recompute_sama_dengan_parser(self):
        ber_id = [""] * len(BRACKET_HEADER)
        ber_id[0], ber_id[4], ber_id[6] = "27/06/2026", "Deposit D1234567 budi", "budi"
        ber_id[11], ber_id[15], ber_id[18] = "50000", "Deposit", "TX-1"
        manual = [""] * len(BRACKET_HEADER)
        manual[0], manual[1], manual[4] = "27/06/2026", "10:00", "Beban Admin QRIS"
        manual[11], manual[15] = "-5000", "Beban Admin QRIS"
        # Ticket panjang 11 digit — TICKET_RE baru menangkapnya (dulu lolos).
        panjang = [""] * len(BRACKET_HEADER)
        panjang[0], panjang[4], panjang[6] = "27/06/2026", "Deposit D12345678901 andi", "andi"
        panjang[11], panjang[15], panjang[18] = "60000", "Deposit", "TX-2"
        rows = _bracket_rows([ber_id, manual, panjang])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["ticket_no"], "D12345678901")
        for row in rows:
            self.assertEqual(
                recompute_bracket_hash(row["raw"]), row["row_hash"],
                f"drift utk baris {row['raw'].get('Description')}",
            )


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


def _bca_parse(lines):
    with patch("sources.parsers.bca_pdf.pdfplumber.open",
               return_value=_FakePDF("\n".join(lines))):
        return BCAPDFParser().parse("/fake.pdf")


T1 = "01/07/2026 TRSF E-BANKING CR 100,000.00BUDI SANTOSO 100,000.00 CR"
T2 = "02/07/2026 TRSF E-BANKING DB 50,000.00SITI AMINAH 50,000.00 DB"
FEE = "01/07/2026 BIAYA TXN tra INTERNET MYBCA 6,500.00 DB"
TRF = "01/07/2026 TRF BUDI SANTOSO 535 MYBCA 93,500.00 DB"


class BcaPdfRecomputeTests(SimpleTestCase):
    def test_recompute_sama_dengan_parser_termasuk_kembar(self):
        rows = _bca_parse([T1, T1, T2])
        self.assertEqual(len(rows), 3)
        got = recompute_bca_pdf_hashes(
            [(i, r["raw"]) for i, r in enumerate(rows)]
        )
        for i, r in enumerate(rows):
            self.assertTrue(is_bca_pdf_raw(r["raw"]))
            self.assertEqual(got[i], r["row_hash"], f"drift baris {i}")

    def test_recompute_baris_switching_merged(self):
        # Pasangan fee+TRF di-merge parser jadi satu WD bruto; hash-nya dihitung
        # SEBELUM merge dari nominal NET di line — recompute dari raw harus sama.
        rows = _bca_parse([FEE, TRF, T2])
        self.assertEqual(len(rows), 2)  # fee lenyap ke baris merged
        self.assertEqual(rows[0]["amount"], Decimal("100000.00"))
        got = recompute_bca_pdf_hashes(
            [(i, r["raw"]) for i, r in enumerate(rows)]
        )
        for i, r in enumerate(rows):
            self.assertEqual(got[i], r["row_hash"], f"drift baris merged {i}")


def _rpay_parse(rows, header):
    fd, p = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    try:
        return RPayGatewayParser().parse(p)
    finally:
        os.remove(p)


RPAY_HEADER = ["UUID", "Date", "Amount", "Fee", "Status",
               "Customer Username", "Customer Name", "RRN"]


class RpayRecomputeTests(SimpleTestCase):
    def test_recompute_sama_dengan_parser(self):
        rows = _rpay_parse([{
            "UUID": "abc-123", "Date": "09/07/2026 10:00", "Amount": "25000.00",
            "Fee": "500", "Status": "SUCCESS", "Customer Username": "budi88",
            "Customer Name": "Budi", "RRN": "999",
        }], RPAY_HEADER)
        self.assertEqual(len(rows), 1)
        raw = rows[0]["raw"]
        self.assertTrue(is_rpay_dp_raw(raw))
        self.assertEqual(recompute_rpay_dp_hash(raw), rows[0]["row_hash"])

    def test_rpay_wd_dan_xlsx_tidak_teridentifikasi(self):
        self.assertFalse(is_rpay_dp_raw(
            {"UUID": "x", "External ID": "W1", "Transfer Status": "Success"}
        ))
        self.assertFalse(is_rpay_dp_raw({"Ticket Number": "D1", "RRN": "9"}))


class RecomputeAllTests(TestCase):
    """Penerapan di tabel: baris ber-hash formula LAMA di-update ke formula
    baru; collision (hash baru sudah dipakai baris lain) dilewati tanpa
    menghapus data."""

    def setUp(self):
        self.toko = Toko.objects.get(key="lbs")
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.gateway = SourceType.objects.get_or_create(key="gateway", defaults={"name": "Gateway"})[0]
        self.bracket = SourceType.objects.get_or_create(key="bracket", defaults={"name": "Bracket"})[0]
        self.up = Upload.objects.create(source_type=self.bank, toko=self.toko)

    def _tx(self, st, rh, raw, up=None, **kw):
        base = dict(
            jenis="depo", amount=Decimal("1000"), money_delta=Decimal("1000"),
            occurred_at=datetime(2026, 6, 27, 10, 0),
        )
        base.update(kw)
        return Transaction.objects.create(
            upload=up or self.up, source_type=st, toko=self.toko,
            row_hash=rh, raw=raw, **base,
        )

    def test_bracket_manual_diupdate(self):
        raw = {"Transaction ID": "", "Description": "Beban Admin QRIS",
               "Username": "", "Total": "-5000",
               "Tanggal": "27/06/2026", "Jam": "10:00"}
        # Formula LAMA: tanpa pembeda Tanggal/Jam/Description.
        old = row_hash("bracket", [None, "", "", Decimal("5000")])
        t = self._tx(self.bracket, old, raw)
        stats = recompute_all(Transaction)
        t.refresh_from_db()
        self.assertEqual(t.row_hash, recompute_bracket_hash(raw))
        self.assertNotEqual(t.row_hash, old)
        self.assertEqual(stats["updated"], 1)

    def test_rpay_collision_dilewati(self):
        raw = {"UUID": "abc", "Customer Username": "budi88"}
        # Dua baris UUID sama yang dulu lolos dedup karena varian format nominal.
        a = self._tx(self.gateway, row_hash("rpay", ["abc", "25000"]), raw)
        b = self._tx(self.gateway, row_hash("rpay", ["abc", "25000.00"]), raw)
        stats = recompute_all(Transaction)
        a.refresh_from_db()
        b.refresh_from_db()
        baru = row_hash("rpay", ["abc"])
        # Tepat satu yang mendapat hash baru; satunya tetap (collision, tak dihapus).
        self.assertEqual(
            sorted([a.row_hash == baru, b.row_hash == baru]), [False, True]
        )
        self.assertEqual(stats["collision"], 1)
        self.assertEqual(Transaction.objects.count(), 2)

    def test_bca_pdf_kembar_occ_urut_id(self):
        raw = {"date": "01/07/2026",
               "line": "TRSF E-BANKING CR 100,000.00BUDI 100,000.00 CR",
               "cont": ""}
        old1 = row_hash("bca_pdf", ["01/07/2026", "x", "CR", "d", 0])
        old2 = row_hash("bca_pdf", ["01/07/2026", "x", "CR", "d", 5])
        a = self._tx(self.bank, old1, dict(raw))
        b = self._tx(self.bank, old2, dict(raw))
        recompute_all(Transaction)
        a.refresh_from_db()
        b.refresh_from_db()
        expected = recompute_bca_pdf_hashes([(1, raw), (2, raw)])
        self.assertEqual(a.row_hash, expected[1])  # occ 0
        self.assertEqual(b.row_hash, expected[2])  # occ 1
        self.assertNotEqual(a.row_hash, b.row_hash)

    def test_baris_lain_tak_disentuh(self):
        t = self._tx(self.bank, "hash-bri-lama",
                     {"NOREK": "123", "Saldo": "100"})
        stats = recompute_all(Transaction)
        t.refresh_from_db()
        self.assertEqual(t.row_hash, "hash-bri-lama")
        self.assertEqual(stats["updated"], 0)


class RemapBerantaiTests(TestCase):
    """W7-2: remap BERANTAI — baris B pindah ke hash yang baru DITINGGALKAN
    baris A pada run yang sama (idx global bca_pdf memampat ke occurrence:
    old idx A=1 → occ 0, old idx B=7 → occ 1 == hash lama A). Postgres
    mengecek unique constraint per-baris (non-deferrable) di dalam SATU
    statement UPDATE dan urutan tulisnya tak dijamin: klaim B bisa dicek
    selagi A belum pindah → unique violation. Penerapan wajib DUA FASE
    (parkir semua ke hash sementara unik → tulis hash final), dan hash
    sementara tak boleh bocor ke hasil akhir."""

    def setUp(self):
        self.toko = Toko.objects.get(key="lbs")
        self.bank = SourceType.objects.get_or_create(
            key="bank", defaults={"name": "Bank"}
        )[0]
        self.up = Upload.objects.create(source_type=self.bank, toko=self.toko)

    def _tx(self, rh, raw):
        return Transaction.objects.create(
            upload=self.up, source_type=self.bank, toko=self.toko,
            jenis="depo", amount=Decimal("1000"), money_delta=Decimal("1000"),
            occurred_at=datetime(2026, 6, 27, 10, 0), row_hash=rh, raw=raw,
        )

    def test_remap_berantai_dua_fase_tanpa_bocor_temp(self):
        raw = {"date": "01/07/2026",
               "line": "TRSF E-BANKING CR 100,000.00BUDI 100,000.00 CR",
               "cont": ""}
        exp = recompute_bca_pdf_hashes([(1, dict(raw)), (2, dict(raw))])
        baru_a, baru_b = exp[1], exp[2]  # occ 0, occ 1
        # Rantai: baris A (pk kecil) SEDANG memegang hash final milik B —
        # hash lama A (idx global 1) == hash baru B (occ 1). A harus pindah
        # dulu (baru_b→baru_a) sebelum B boleh klaim baru_b.
        a = self._tx(baru_b, dict(raw))
        b = self._tx("hash-idx-global-lama-7", dict(raw))

        from django.db.models import QuerySet

        pelanggaran = []
        asli = QuerySet.bulk_update

        def cek_unique_per_baris(qs, objs, fields, **kw):
            # Simulasi constraint unique NON-deferrable: hash yang ditulis
            # dalam satu statement tak boleh sama dgn hash TERSIMPAN baris
            # LAIN (pre-state) — persis kondisi yang membuat Postgres bisa
            # menolak tergantung urutan tulis internal.
            objs = list(objs)
            if "row_hash" in fields:
                pra = dict(Transaction.objects.values_list("pk", "row_hash"))
                for o in objs:
                    pemilik = {pk for pk, h in pra.items()
                               if h == o.row_hash and pk != o.pk}
                    if pemilik:
                        pelanggaran.append((o.pk, o.row_hash, pemilik))
            return asli(qs, objs, fields, **kw)

        with patch.object(QuerySet, "bulk_update", cek_unique_per_baris):
            stats = recompute_all(Transaction)

        self.assertEqual(pelanggaran, [])  # tak ada klaim hash yang masih dipegang
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.row_hash, baru_a)
        self.assertEqual(b.row_hash, baru_b)  # rantai selesai di hash final
        self.assertEqual(stats["updated"], 2)
        # Hash sementara tak pernah bocor ke hasil akhir.
        self.assertFalse(
            Transaction.objects.filter(row_hash__startswith="mig0010:").exists()
        )
