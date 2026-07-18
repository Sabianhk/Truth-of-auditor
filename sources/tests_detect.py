import os
import tempfile

import openpyxl
from django.test import SimpleTestCase

from sources.detect import detect_source, _pdf_key


def _xlsx(rows):
    fd, p = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(p)
    return p


def _csv(text):
    fd, p = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


class DetectMultiBrandTests(SimpleTestCase):
    def _mk(self, header, name):
        path = _xlsx([header, ["x"] * len(header)])
        return path, name

    def test_deteksi_cor_qris_gateway(self):
        path = _xlsx([["BranchName", "GrandTotal", "BranchNominal", "OrderId",
                       "TransactionTime", "RRN"], ["a", "1", "1", "u", "t", "r"]])
        try:
            keys = [d["parser_key"] for d in detect_source(path, "01 DP_QRIS_TRANSACTION.xlsx")]
        finally:
            os.remove(path)
        self.assertEqual(keys[0], "cor_qris_gateway")

    def test_deteksi_qhoki(self):
        path = _xlsx([["Member ID", "Whitelabel Transaction ID", "NMID",
                       "Transaction ID", "Status", "Amount"], ["m", "D1", "", "u", "Success", "1"]])
        try:
            keys = [d["parser_key"] for d in detect_source(path, "DP QH MUL.xlsx")]
        finally:
            os.remove(path)
        self.assertIn("qhoki", keys)

    def test_deteksi_cor_panel_bank(self):
        path = _xlsx([["Approved Date", "Requested Date", "Username", "From Bank",
                       "Destination Bank", "Amount", "Status"], ["a"] * 7])
        try:
            keys = [d["parser_key"] for d in detect_source(path, "BANK_approved_deposit.xlsx")]
        finally:
            os.remove(path)
        self.assertIn("cor_panel_bank", keys)

    def test_deteksi_cor_panel_qris(self):
        path = _xlsx([["#", "Approved Date", "Requested Date", "Username",
                       "Transaction ID", "Amount", "Bonus", "Status"],
                      ["1", "a", "a", "u", "t", "1", "", "success"]])
        try:
            keys = [d["parser_key"] for d in detect_source(path, "DP_QRIS_PANEL.xlsx")]
        finally:
            os.remove(path)
        self.assertIn("cor_panel_qris", keys)

    def test_deteksi_cor_panel_qris_withdraw_tanpa_bonus(self):
        path = _xlsx([["#","Approved Date","Requested Date","Username","Transaction ID",
                       "Destination Bank","Amount","Status","By"],
                      ["1","x","x","user1","uuid-1","BCA - 1 - A","50000","success","op"]])
        try:
            keys = [d["parser_key"] for d in detect_source(path, "QRIS_withdraw_transactions.xlsx")]
        finally:
            os.remove(path)
        self.assertEqual(keys[0], "cor_panel_qris")

    def test_qhoki_dan_cor_panel_qris_tidak_tabrakan(self):
        # Header asli QHoki (Task 8 brief): py qhoki punya "transaction id" + "nmid",
        # TANPA "bonus" -> tidak boleh ikut kena deteksi cor_panel_qris.
        path = _xlsx([["Transaction Date", "Paid Date", "Finished Date", "Settlement Date",
                       "Settled At", "Member ID", "Rrn", "NMID", "Transaction ID",
                       "Whitelabel Transaction ID", "Status", "Amount", "Downline Fee Amount",
                       "Total Amount", "Memo", "Payment Method"],
                      ["2026-07-03", "", "", "", "", "m", "r", "", "u", "D1",
                       "Success", "1", "0", "1", "", "qris"]])
        try:
            keys = [d["parser_key"] for d in detect_source(path, "DP QH MUL.xlsx")]
        finally:
            os.remove(path)
        self.assertIn("qhoki", keys)
        self.assertNotIn("cor_panel_qris", keys)

    def test_cor_panel_qris_dan_qhoki_tidak_tabrakan(self):
        # Header asli COR Panel QRIS (Task 8 brief): punya "bonus", TANPA "nmid"/
        # "whitelabel transaction id" -> tidak boleh ikut kena deteksi qhoki.
        path = _xlsx([["#", "Approved Date", "Requested Date", "Username",
                       "Transaction ID", "Amount", "Bonus", "Status"],
                      ["1", "a", "a", "u", "t", "1", "", "success"]])
        try:
            keys = [d["parser_key"] for d in detect_source(path, "DP_QRIS_PANEL.xlsx")]
        finally:
            os.remove(path)
        self.assertIn("cor_panel_qris", keys)
        self.assertNotIn("qhoki", keys)


class DetectTests(SimpleTestCase):
    def test_panel(self):
        p = _xlsx([["HISTORI DP PANEL"], ["Ticket Number", "User Name", "Deposit Amount"]])
        self.assertEqual(detect_source(p, "hist.xlsx")[0]["parser_key"], "panel")

    def test_nxpay_not_confused_with_panel(self):
        p = _xlsx([["NXPAY REPORT"], ["Ticket Number", "Username", "Amount", "Admin Fee", "Account Title"]])
        self.assertEqual(detect_source(p, "nx.xlsx")[0]["parser_key"], "nxpay")

    def test_bracket(self):
        p = _xlsx([["Kategori", "Credit Awal", "Credit Akhir", "Transaction ID"]])
        self.assertEqual(detect_source(p, "fr.xlsx")[0]["parser_key"], "bracket")

    def test_qrflyer(self):
        # header asli QR FLYER: tanpa token "QRIS"/"QR FLYER" di dalam file
        p = _xlsx([[
            "Transaction Date", "Client Reference", "TXN ID",
            "Customer ID / User Account", "Payment Status", "Settlement Time",
            "System Processed At", "Transaction Value",
        ]])
        self.assertEqual(detect_source(p, "MUTASI DP QR FLYER OKE25 28-06.xlsx")[0]["parser_key"], "qrflyer")

    def test_bri_csv(self):
        p = _csv("TGL_TRAN,MUTASI_DEBET,MUTASI_KREDIT,DESK_TRAN\n")
        self.assertEqual(detect_source(p, "bri.csv")[0]["parser_key"], "bri")

    def test_bca_csv(self):
        p = _csv("Rekening\nTanggal,Keterangan,Cabang,Jumlah,,Saldo\n")
        self.assertEqual(detect_source(p, "bca.csv")[0]["parser_key"], "bca_csv")

    def test_pdf_extension(self):
        fd, p = tempfile.mkstemp(suffix=".pdf")
        os.close(fd)
        self.assertEqual(detect_source(p, "koran.pdf")[0]["parser_key"], "bca_pdf")

    def test_unknown_returns_empty(self):
        p = _xlsx([["Foo", "Bar"]])
        self.assertEqual(detect_source(p, "x.xlsx"), [])


class FilenameOnlySignalTests(SimpleTestCase):
    """Sinyal nama-file-murni (tanpa dukungan header) tak boleh auto-confirm.

    Ambang konfirmasi upload = 0.8; skor 0.85 dari sekadar nama file pernah
    merutekan file ke parser salah tanpa konfirmasi (kelas bug nyata 16-07).
    """

    def test_nama_qris_header_asing_di_bawah_ambang(self):
        path = _xlsx([["Foo", "Bar"], ["x", "y"]])
        try:
            hasil = detect_source(path, "REKAP QRIS JULI.xlsx")
        finally:
            os.remove(path)
        self.assertEqual(hasil[0]["parser_key"], "qrflyer")
        self.assertLess(hasil[0]["confidence"], 0.8)

    def test_nama_mandiri_header_asing_di_bawah_ambang(self):
        path = _xlsx([["Foo", "Bar"], ["x", "y"]])
        try:
            hasil = detect_source(path, "mutasi mandiri juli.xlsx")
        finally:
            os.remove(path)
        self.assertEqual(hasil[0]["parser_key"], "mandiri")
        self.assertLess(hasil[0]["confidence"], 0.8)

    def test_nama_bca_isi_asing_di_bawah_ambang(self):
        path = _csv("kolom1,kolom2\nnilai1,nilai2\n")
        try:
            hasil = detect_source(path, "rekap bca juli.csv")
        finally:
            os.remove(path)
        self.assertEqual(hasil[0]["parser_key"], "bca_csv")
        self.assertLess(hasil[0]["confidence"], 0.8)

    def test_header_qris_di_dalam_file_tetap_auto(self):
        # Token "qris" DI DALAM file = sinyal isi, bukan nama-file-murni.
        path = _xlsx([["Laporan QRIS"], ["kolomA", "kolomB"]])
        try:
            hasil = detect_source(path, "laporan.xlsx")
        finally:
            os.remove(path)
        self.assertEqual(hasil[0]["parser_key"], "qrflyer")
        self.assertGreaterEqual(hasil[0]["confidence"], 0.8)

    def test_header_bca_csv_tetap_auto(self):
        path = _csv("Rekening\nTanggal,Keterangan,Cabang,Jumlah,,Saldo\n")
        try:
            hasil = detect_source(path, "mutasi.csv")
        finally:
            os.remove(path)
        self.assertEqual(hasil[0]["parser_key"], "bca_csv")
        self.assertGreaterEqual(hasil[0]["confidence"], 0.8)

    def test_header_estatement_mandiri_tetap_auto(self):
        path = _xlsx([["e-Statement"], ["No", "Tanggal", "Keterangan"]])
        try:
            hasil = detect_source(path, "laporan.xlsx")
        finally:
            os.remove(path)
        self.assertEqual(hasil[0]["parser_key"], "mandiri")
        self.assertGreaterEqual(hasil[0]["confidence"], 0.8)


class PDFKeyRoutingTests(SimpleTestCase):
    def test_bni_dari_teks(self):
        txt = ("HISTORI TRANSAKSI\nRekening: TAPLUS DIGITAL\n"
               "Tanggal Uraian Transaksi Tipe Nominal Saldo Akhir")
        self.assertEqual(_pdf_key(txt), "bni_pdf")

    def test_bca_default(self):
        txt = "MUTASI REKENING\nNO. REKENING : 712-6201-591\nNAMA : HENDI"
        self.assertEqual(_pdf_key(txt), "bca_pdf")

    def test_teks_kosong_default_bca(self):
        self.assertEqual(_pdf_key(""), "bca_pdf")


class DetectRPayXlsxTests(SimpleTestCase):
    """RafflesPay varian XLSX (BBS) — dulu nyasar ke nxpay/qrflyer (bug nyata 16-07)."""

    DP_HEADER = ["Website", "Date", "Ticket Number", "Player", "Payment Type",
                 "Account Title", "Status", "Payment Gateway", "RRN",
                 "Amount (IDR)", "Amount (Chip)", "Player Fee", "Agent Fee",
                 "Admin Fee", "Player Nett Amount", "Agent Nett Amount",
                 "Ticket Status", "Promotion"]
    WD_TOP = ["ID", "Website", "Date", "Ticket", "Player", "Source of Funds",
              "Beneficiary", "", "", "Amount", "", "", "Status", "", "", ""]
    WD_SUB = ["", "", "", "", "", "", "Bank", "Name", "Number", "Amount",
              "Disbursed Amount", "Fee", "Status", "Approve", "Reject", "Transfer"]

    def test_dp_rpay_xlsx_menang_atas_nxpay(self):
        # Nama file mengandung QRIS -> dulu qrflyer 0.85 & header mirip nxpay 0.90.
        path = _xlsx([self.DP_HEADER, ["x"] * len(self.DP_HEADER)])
        try:
            hasil = detect_source(path, "16_07_2026_BBS_DP_QRIS_RPAY_CSV.xlsx")
        finally:
            os.remove(path)
        self.assertEqual(hasil[0]["parser_key"], "rpay_xlsx")
        self.assertGreaterEqual(hasil[0]["confidence"], 0.95)
        self.assertNotIn("nxpay", [d["parser_key"] for d in hasil])

    def test_wd_rpay_xlsx_menang_atas_qrflyer(self):
        path = _xlsx([self.WD_TOP, self.WD_SUB, ["x"] * len(self.WD_TOP)])
        try:
            hasil = detect_source(path, "16_07_2026_BBS_WD_QRIS_RPAY.xlsx")
        finally:
            os.remove(path)
        self.assertEqual(hasil[0]["parser_key"], "rpay_wd_xlsx")
        self.assertGreaterEqual(hasil[0]["confidence"], 0.95)

    def test_nxpay_asli_tetap_terdeteksi(self):
        # Non-regresi: header NXPay (tanpa 'Payment Gateway') tetap nxpay.
        path = _xlsx([["judul report"], ["Ticket Number", "Username", "Amount",
                      "Date", "Admin Fee", "Account Title"], ["t", "u", 1, "d", 0, "a"]])
        try:
            hasil = detect_source(path, "nxpay.xlsx")
        finally:
            os.remove(path)
        self.assertEqual(hasil[0]["parser_key"], "nxpay")
