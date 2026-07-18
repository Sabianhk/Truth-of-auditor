"""Parser gateway UNO WD (QRIS withdrawal Vigor/TMG) & RPay (QRIS DP Nexus/MUL)."""
import os, tempfile
from django.test import SimpleTestCase
from openpyxl import Workbook


def _xlsx(rows):
    wb = Workbook(); ws = wb.active
    for r in rows:
        ws.append(r)
    fd, path = tempfile.mkstemp(suffix=".xlsx"); os.close(fd)
    wb.save(path)
    return path


UNO_WD_HEADER = ["Merchant Name", "Order ID (Merchant)", "AccountNumber",
                 "RecipientName", "Grand Total", "Amount", "Fee", "Remark",
                 "TransactionTime", "Status"]


class UnoWDGatewayTests(SimpleTestCase):
    def _parse(self, rows):
        from sources.parsers.cor import CORQRISWDGatewayParser
        path = _xlsx([UNO_WD_HEADER] + rows)
        try:
            return CORQRISWDGatewayParser().parse(path)
        finally:
            os.remove(path)

    def test_wd_sukses_field_lengkap(self):
        rows = self._parse([
            ["Omega Vig66", "fd1a26d3-5dbe-411b-9f32-96e97184fe31", "081270553953",
             "081270553953", "800900", "800000", "900", "[via-api] ",
             "2026-07-03 23:54:40", "SUCCESS"],
        ])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["jenis"], "wd")
        self.assertEqual(str(r["amount"]), "800000")       # nett = angka panel
        self.assertEqual(str(r["money_delta"]), "-800000")
        self.assertEqual(str(r["credit_delta"]), "0")
        self.assertEqual(str(r["fee"]), "900")
        self.assertEqual(r["reference"], "fd1a26d3-5dbe-411b-9f32-96e97184fe31")
        self.assertEqual(r["counterparty"], "")            # recipient == account (telepon)
        self.assertEqual(r["occurred_at"].hour, 23)
        self.assertIn("081270553953", r["raw"]["AccountNumber"])

    def test_refund_dilewati(self):
        rows = self._parse([
            ["Omega Vig66", "6f2ebccd-9da1-47be-8986-36065e520fc2", "901829968671",
             "901829968671", "412110", "410610", "1500", "[via-api] ",
             "2026-07-03 23:11:52", "REFUND"],
        ])
        self.assertEqual(rows, [])

    def test_transfer_manual_non_uuid_tetap_diambil(self):
        rows = self._parse([
            ["Omega Vig66", "ee4c1d014ae6451891ad", "058801037091503",
             "MAULANA IQBAL AILA", "30001500", "30000000", "1500", "0",
             "2026-07-03 21:20:14", "SUCCESS"],
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["counterparty"], "MAULANA IQBAL AILA")

    def test_row_hash_stabil(self):
        baris = ["Omega Vig66", "fd1a26d3-5dbe-411b-9f32-96e97184fe31", "081270553953",
                 "081270553953", "800900", "800000", "900", "", "2026-07-03 23:54:40", "SUCCESS"]
        a = self._parse([baris])[0]["row_hash"]
        b = self._parse([baris])[0]["row_hash"]
        self.assertEqual(a, b)


class UnoWDRegistrationTests(SimpleTestCase):
    def test_terdaftar_di_parsers(self):
        from sources.services import PARSERS
        from sources.parsers.cor import CORQRISWDGatewayParser
        self.assertIs(PARSERS.get("cor_qris_wd_gateway"), CORQRISWDGatewayParser)

    def test_terdeteksi_dari_header(self):
        from sources.detect import detect_source
        path = _xlsx([UNO_WD_HEADER,
                      ["Omega Vig66", "fd1a26d3-5dbe-411b-9f32-96e97184fe31",
                       "081270553953", "081270553953", "800900", "800000", "900",
                       "", "2026-07-03 23:54:40", "SUCCESS"]])
        try:
            ranked = detect_source(path, "MUTASI WD QR UNO SLO 03-07.xlsx")
        finally:
            os.remove(path)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0]["parser_key"], "cor_qris_wd_gateway")
        self.assertGreaterEqual(ranked[0]["confidence"], 0.9)


RPAY_HEADER = ("No.,Merchant,Customer Name,Customer Username,Date,UUID,"
               "External ID,RRN,Acquirer Merchant,Time,Elapsed Time (s),Amount,Fee,Status")


def _csv(lines):
    fd, path = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


class RPayGatewayTests(SimpleTestCase):
    def _parse(self, lines, flow=""):
        from sources.parsers.gateways import RPayGatewayParser
        path = _csv([RPAY_HEADER] + lines)
        try:
            return RPayGatewayParser().parse(path, flow=flow)
        finally:
            os.remove(path)

    def test_dp_sukses_field_lengkap(self):
        rows = self._parse([
            '1,NOMINA ISI ULANG,kaleng1,kaleng1,"09 Jul 2026, 23:59",'
            '93c8f884-bd54-445f-96df-e899a660cb64,46645580,619180666745,'
            'Thundfire Game,49s,49,25000.0,325.0,success',
        ])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["jenis"], "depo")
        self.assertEqual(str(r["amount"]), "25000.0")
        self.assertGreater(r["money_delta"], 0)
        self.assertEqual(r["username"], "kaleng1")
        self.assertEqual(r["reference"], "")   # sengaja: aturan blocked engine
        self.assertEqual(r["raw"]["UUID"], "93c8f884-bd54-445f-96df-e899a660cb64")
        self.assertEqual(r["counterparty"], "")  # Customer Name == Username
        self.assertEqual((r["occurred_at"].year, r["occurred_at"].month,
                          r["occurred_at"].day, r["occurred_at"].hour,
                          r["occurred_at"].minute), (2026, 7, 9, 23, 59))

    def test_non_success_dilewati(self):
        rows = self._parse([
            '2,NOMINA ISI ULANG,irma30,irma30,"09 Jul 2026, 23:59",'
            '8d422e0c-eb9c-4baa-a310-544055a7bac7,46645575,000139896397,'
            'Frostcry Game,45s,45,50000.0,650.0,failed',
        ])
        self.assertEqual(rows, [])

    def test_row_hash_stabil_dan_unik_per_uuid(self):
        a = ('1,NOMINA ISI ULANG,kaleng1,kaleng1,"09 Jul 2026, 23:59",'
             '93c8f884-bd54-445f-96df-e899a660cb64,46645580,619180666745,'
             'Thundfire Game,49s,49,25000.0,325.0,success')
        b = ('2,NOMINA ISI ULANG,irma30,irma30,"09 Jul 2026, 23:59",'
             '8d422e0c-eb9c-4baa-a310-544055a7bac7,46645575,000139896397,'
             'Frostcry Game,45s,45,25000.0,325.0,success')
        h1 = self._parse([a])[0]["row_hash"]
        h1b = self._parse([a])[0]["row_hash"]
        h2 = self._parse([b])[0]["row_hash"]
        self.assertEqual(h1, h1b)
        self.assertNotEqual(h1, h2)

    def test_row_hash_kebal_variasi_format_nominal(self):
        # Hash = UUID saja (pola sama rpay_wd): "25000" vs "25000.00" adalah
        # transaksi yang SAMA — variasi format angka ekspor tak boleh
        # menggoyahkan idempotensi.
        a = ('1,NOMINA ISI ULANG,kaleng1,kaleng1,"09 Jul 2026, 23:59",'
             '93c8f884-bd54-445f-96df-e899a660cb64,46645580,619180666745,'
             'Thundfire Game,49s,49,25000,325.0,success')
        b = ('1,NOMINA ISI ULANG,kaleng1,kaleng1,"09 Jul 2026, 23:59",'
             '93c8f884-bd54-445f-96df-e899a660cb64,46645580,619180666745,'
             'Thundfire Game,49s,49,25000.00,325.0,success')
        self.assertEqual(
            self._parse([a])[0]["row_hash"], self._parse([b])[0]["row_hash"]
        )


class RPayHardeningTests(SimpleTestCase):
    """Temuan review codex: normalisasi tanda & tanggal ambigu."""

    def _parse(self, lines, flow=""):
        from sources.parsers.gateways import RPayGatewayParser
        path = _csv([RPAY_HEADER] + lines)
        try:
            return RPayGatewayParser().parse(path, flow=flow)
        finally:
            os.remove(path)

    def test_nominal_negatif_dinormalkan_abs(self):
        # Konsisten dgn parser gateway lain: tanda ditentukan flow, bukan file.
        rows = self._parse([
            '1,NOMINA ISI ULANG,kaleng1,kaleng1,"09 Jul 2026, 23:59",'
            '93c8f884-bd54-445f-96df-e899a660cb64,46645580,619180666745,'
            'Thundfire Game,49s,49,-25000.0,325.0,success',
        ])
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["money_delta"], 0)   # depo tetap uang masuk
        self.assertGreater(rows[0]["amount"], 0)

    def test_tanggal_numerik_dibaca_dayfirst(self):
        # Vendor Indonesia: 09/07/2026 = 9 Juli, BUKAN 7 September.
        rows = self._parse([
            '1,NOMINA ISI ULANG,kaleng1,kaleng1,09/07/2026 23:59,'
            '93c8f884-bd54-445f-96df-e899a660cb64,46645580,619180666745,'
            'Thundfire Game,49s,49,25000.0,325.0,success',
        ])
        self.assertEqual((rows[0]["occurred_at"].month, rows[0]["occurred_at"].day), (7, 9))


# Laporan WD/disbursement RafflesPay (BBS/BO7) — format BEDA dari RPay DP:
# TANPA Customer Username, kunci = External ID (tiket W...) == Ticket Number panel.
RPAY_WD_HEADER = ("No.,Merchant,Date,UUID,External ID,Bank Name,Account Name,"
                  "Account Number,Requested Amount,Disbursed Amount,Fee,"
                  "Approval Status,Transfer Status")


class RPayWDGatewayTests(SimpleTestCase):
    def _parse(self, lines, flow="wd"):
        from sources.parsers.gateways import RPayWDGatewayParser
        path = _csv([RPAY_WD_HEADER] + lines)
        try:
            return RPayWDGatewayParser().parse(path, flow=flow)
        finally:
            os.remove(path)

    def test_wd_sukses_ticket_anchor(self):
        rows = self._parse([
            '1,BOBA MINUMAN SEGAR,"12 Jul 2026, 17:57",'
            '316bb4a5-3ebe-479a-abb7-e7eb50679984,W2546823,BCA,'
            'HERDIANSYAH KAMARUZZAMAN,2230090087,1000000.0,1000000.0,5000.0,'
            'Approved,Success',
        ])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["jenis"], "wd")
        self.assertEqual(r["ticket_no"], "W2546823")     # == tiket panel WD -> pass 0
        self.assertEqual(str(r["amount"]), "1000000.0")  # Disbursed = uang riil keluar
        self.assertLess(r["money_delta"], 0)             # WD = uang keluar
        self.assertEqual(str(r["credit_delta"]), "0")
        self.assertEqual(str(r["fee"]), "5000.0")
        self.assertEqual(r["reference"], "")             # UUID di raw saja (aturan blocked)
        self.assertEqual(r["raw"]["UUID"], "316bb4a5-3ebe-479a-abb7-e7eb50679984")
        self.assertEqual(r["counterparty"], "HERDIANSYAH KAMARUZZAMAN")
        self.assertEqual(r["username"], "")
        self.assertEqual((r["occurred_at"].month, r["occurred_at"].day,
                          r["occurred_at"].hour), (7, 12, 17))

    def test_transfer_gagal_dilewati(self):
        rows = self._parse([
            '1,BOBA MINUMAN SEGAR,"12 Jul 2026, 17:57",uuidx,W2546823,BCA,'
            'NAMA,2230090087,1000000.0,1000000.0,5000.0,Approved,Failed',
        ])
        self.assertEqual(rows, [])

    def test_transfer_pending_dilewati(self):
        # Transfer belum sukses = uang belum keluar -> jangan dihitung.
        rows = self._parse([
            '1,BOBA MINUMAN SEGAR,"12 Jul 2026, 17:57",uuidx,W2546823,BCA,'
            'NAMA,2230090087,1000000.0,1000000.0,5000.0,Approved,Pending',
        ])
        self.assertEqual(rows, [])

    def test_selalu_wd_walau_flow_dp(self):
        # Laporan disbursement tak pernah deposit: salah-pilih flow=dp di UI
        # TIDAK boleh membalik tanda jadi uang masuk (hardening review codex).
        rows = self._parse([
            '1,BOBA MINUMAN SEGAR,"12 Jul 2026, 17:57",'
            '316bb4a5-3ebe-479a-abb7-e7eb50679984,W2546823,BCA,NAMA,223,'
            '1000000.0,1000000.0,5000.0,Approved,Success',
        ], flow="dp")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["jenis"], "wd")
        self.assertLess(rows[0]["money_delta"], 0)   # tetap uang keluar

    def test_row_hash_stabil_dan_unik(self):
        a = ('1,BOBA MINUMAN SEGAR,"12 Jul 2026, 17:57",'
             '316bb4a5-3ebe-479a-abb7-e7eb50679984,W2546823,BCA,NAMA,223,'
             '1000000.0,1000000.0,5000.0,Approved,Success')
        b = ('2,BOBA MINUMAN SEGAR,"12 Jul 2026, 17:12",'
             '48fa2c52-a20d-4b61-b0ec-7298642c4510,W2546782,SEABANK,X,901,'
             '1750000.0,1750000.0,5000.0,Approved,Success')
        self.assertEqual(self._parse([a])[0]["row_hash"], self._parse([a])[0]["row_hash"])
        self.assertNotEqual(self._parse([a])[0]["row_hash"], self._parse([b])[0]["row_hash"])


class RPayWDRegistrationTests(SimpleTestCase):
    def test_terdaftar_di_parsers(self):
        from sources.services import PARSERS
        from sources.parsers.gateways import RPayWDGatewayParser
        self.assertIs(PARSERS.get("rpay_wd"), RPayWDGatewayParser)

    def test_terdeteksi_dari_header_csv(self):
        from sources.detect import detect_source
        path = _csv([RPAY_WD_HEADER,
                     '1,BOBA MINUMAN SEGAR,"12 Jul 2026, 17:57",'
                     '316bb4a5-3ebe-479a-abb7-e7eb50679984,W2546823,BCA,NAMA,223,'
                     '1000000.0,1000000.0,5000.0,Approved,Success'])
        try:
            ranked = detect_source(path, "12_07_2026 BBS WD QRIS RPAY.csv")
        finally:
            os.remove(path)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0]["parser_key"], "rpay_wd")
        self.assertGreaterEqual(ranked[0]["confidence"], 0.9)

    def test_tidak_bentrok_dengan_rpay_dp(self):
        # DP RPay (Customer Username + Acquirer Merchant, TANPA Disbursed Amount)
        # tetap ke-detect sebagai `rpay`, bukan `rpay_wd`.
        from sources.detect import detect_source
        path = _csv([RPAY_HEADER,
                     '1,NOMINA ISI ULANG,kaleng1,kaleng1,"09 Jul 2026, 23:59",'
                     '93c8f884-bd54-445f-96df-e899a660cb64,46645580,619180666745,'
                     'Thundfire Game,49s,49,25000.0,325.0,success'])
        try:
            ranked = detect_source(path, "dp rpay.csv")
        finally:
            os.remove(path)
        self.assertEqual(ranked[0]["parser_key"], "rpay")


class RPayRegistrationTests(SimpleTestCase):
    def test_terdaftar_di_parsers(self):
        from sources.services import PARSERS
        from sources.parsers.gateways import RPayGatewayParser
        self.assertIs(PARSERS.get("rpay"), RPayGatewayParser)

    def test_terdeteksi_dari_header_csv(self):
        from sources.detect import detect_source
        path = _csv([RPAY_HEADER,
                     '1,NOMINA ISI ULANG,kaleng1,kaleng1,"09 Jul 2026, 23:59",'
                     '93c8f884-bd54-445f-96df-e899a660cb64,46645580,619180666745,'
                     'Thundfire Game,49s,49,25000.0,325.0,success'])
        try:
            ranked = detect_source(path, "dp rpay.csv")
        finally:
            os.remove(path)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0]["parser_key"], "rpay")
        self.assertGreaterEqual(ranked[0]["confidence"], 0.9)
