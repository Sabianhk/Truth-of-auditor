"""Aturan anchor UTAMA di matcher uang (Panel/Bracket ↔ Bank).

Nominal + tanggal hanya anchor PENDUKUNG: wajib (memblokir kandidat) tapi tidak
pernah cukup untuk memasangkan. Anchor UTAMA = identitas unik (nama lengkap,
no. HP/VA, no. rekening, username/ticket). Regresi kasus prod: WD "Samsul maarif"
300rb TIDAK boleh dipasangkan ke mutasi "ARI PRIHARTANTO" 300rb hanya karena
nominal+tanggal sama (skor nama 36) — biarkan menunggu settlement H+1.
"""
from datetime import datetime
from decimal import Decimal

from django.test import TestCase

from reconciliation.engine import NAME_REVIEW_FLOOR, NO_MONEY_DETAIL, run_match
from reconciliation.models import MatchResult, ToleranceProfile
from sources.models import SourceType, Upload
from transactions.models import Transaction


class AnchorUtamaMoneyMatcherTests(TestCase):
    def setUp(self):
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bank = SourceType.objects.get_or_create(
            key="bank", defaults={"name": "Bank", "is_money_source": True}
        )[0]
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1, "fuzzy_threshold": 85}
        )[0]
        self.up = Upload.objects.create(source_type=self.panel)
        self.upb = Upload.objects.create(source_type=self.bank)

    def _panel(self, jenis, money, name, rh, dt, **kw):
        return Transaction.objects.create(
            upload=self.up, source_type=self.panel, jenis=jenis,
            amount=Decimal(abs(money)), money_delta=Decimal(money),
            counterparty=name, occurred_at=dt, row_hash=rh, **kw,
        )

    def _bank(self, jenis, money, name, rh, dt, **kw):
        return Transaction.objects.create(
            upload=self.upb, source_type=self.bank, jenis=jenis,
            amount=Decimal(abs(money)), money_delta=Decimal(money),
            counterparty=name, occurred_at=dt, row_hash=rh, **kw,
        )

    def test_wd_nama_beda_tidak_dipasangkan(self):
        # Kasus prod Run #41: nominal+tanggal sama, nama skor 36 → JANGAN pasangkan.
        p = self._panel("wd", -300000, "Samsul maarif", "p1",
                        datetime(2026, 7, 1, 23, 22))
        b = self._bank("wd", -300000, "ARI PRIHARTANTO", "b1",
                       datetime(2026, 7, 1, 0, 0))
        run = run_match("panel_bank", self.tol)
        r = MatchResult.objects.get(run=run, left=p)
        self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)
        self.assertEqual(r.reason_code, "no_money")
        self.assertIsNone(r.right)
        # Baris mutasi milik player lain TIDAK ikut terpakai (tak ada hasil right=b).
        self.assertFalse(MatchResult.objects.filter(run=run, right=b).exists())

    def test_nama_mirip_band_jadi_perlu_tinjau(self):
        # Skor 60–84 (nama kepotong bank): "M ADITYA FIRMANSYA" vs full name.
        p = self._panel("depo", 150000, "Muhammad Aditya Firmansyah", "p1",
                        datetime(2026, 7, 1, 9, 0))
        self._bank("depo", 150000, "M ADITYA FIRMANSYA", "b1",
                   datetime(2026, 7, 1, 10, 0))
        run = run_match("panel_bank", self.tol)
        r = MatchResult.objects.get(run=run, left=p)
        self.assertEqual(r.bucket, MatchResult.Bucket.TINJAU)
        self.assertEqual(r.reason_code, "name_partial")
        self.assertIsNotNone(r.right)

    def test_band_bawah_dan_skor_penuh_batasnya(self):
        # Skor persis di floor tetap tinjau; di bawah floor → no_money.
        self.assertEqual(NAME_REVIEW_FLOOR, 60)

    def test_assignment_global_skor_tertinggi_menang(self):
        # Dua panel berebut satu mutasi; keduanya di band → skor tertinggi menang,
        # yang kalah jatuh ke no_money (bukan dipaksakan ke mutasi yang sama).
        p_hi = self._panel("depo", 150000, "Muhammad Aditya Firmansyah", "phi",
                           datetime(2026, 7, 1, 9, 0))   # skor ~82
        p_lo = self._panel("depo", 150000, "Ade Firmansyah", "plo",
                           datetime(2026, 7, 1, 9, 5))    # skor ~75
        b = self._bank("depo", 150000, "M ADITYA FIRMANSYA", "b1",
                       datetime(2026, 7, 1, 10, 0))
        run = run_match("panel_bank", self.tol)
        r_hi = MatchResult.objects.get(run=run, left=p_hi)
        r_lo = MatchResult.objects.get(run=run, left=p_lo)
        self.assertEqual(r_hi.bucket, MatchResult.Bucket.TINJAU)
        self.assertEqual(r_hi.right, b)
        self.assertEqual(r_lo.bucket, MatchResult.Bucket.TIDAK)
        self.assertEqual(r_lo.reason_code, "no_money")
        self.assertIsNone(r_lo.right)

    def test_no_money_detail_bedakan_ada_kandidat(self):
        # Ada kandidat nominal+tanggal tapi identitas <60 → detail beri sinyal tunggu.
        p1 = self._panel("wd", -300000, "Samsul maarif", "p1",
                         datetime(2026, 7, 1, 23, 22))
        self._bank("wd", -300000, "ARI PRIHARTANTO", "b1",
                   datetime(2026, 7, 1, 0, 0))
        # Tanpa kandidat nominal sama sekali → detail baku NO_MONEY_DETAIL.
        p2 = self._panel("wd", -777000, "Joko Widodo", "p2",
                         datetime(2026, 7, 1, 20, 0))
        run = run_match("panel_bank", self.tol)
        r1 = MatchResult.objects.get(run=run, left=p1)
        r2 = MatchResult.objects.get(run=run, left=p2)
        self.assertEqual(r1.reason_code, "no_money")
        self.assertIn("menunggu settlement", r1.reason_detail.lower())
        self.assertEqual(r2.reason_detail, NO_MONEY_DETAIL)

    def test_weak_name_tidak_diproduksi(self):
        # Reason code lama 'weak_name' tak boleh muncul lagi.
        self._panel("wd", -300000, "Samsul maarif", "p1",
                    datetime(2026, 7, 1, 23, 22))
        self._bank("wd", -300000, "ARI PRIHARTANTO", "b1",
                   datetime(2026, 7, 1, 0, 0))
        run = run_match("panel_bank", self.tol)
        self.assertFalse(
            MatchResult.objects.filter(run=run, reason_code="weak_name").exists()
        )


class AnchorNomorHPTests(TestCase):
    """Anchor nomor HP/VA — mutasi e-wallet (FTFVA/DANA, GO-PAY TOPUP) tak membawa
    nama pengirim tapi membawa nomor tujuan; format persis dari mutasi BCA riil."""

    def setUp(self):
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bank = SourceType.objects.get_or_create(
            key="bank", defaults={"name": "Bank", "is_money_source": True}
        )[0]
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1, "fuzzy_threshold": 85}
        )[0]
        self.up = Upload.objects.create(source_type=self.panel)
        self.upb = Upload.objects.create(source_type=self.bank)

    def _panel(self, money, name, pbank, rh, dt):
        return Transaction.objects.create(
            upload=self.up, source_type=self.panel, jenis="wd",
            amount=Decimal(abs(money)), money_delta=Decimal(money),
            counterparty=name, occurred_at=dt, row_hash=rh,
            raw={"Player Bank": pbank},
        )

    def _bank(self, money, ket, rh, dt):
        return Transaction.objects.create(
            upload=self.upb, source_type=self.bank, jenis="wd",
            amount=Decimal(abs(money)), money_delta=Decimal(money),
            counterparty="", occurred_at=dt, row_hash=rh, raw={"Keterangan": ket},
        )

    def test_dana_va_nomor_cocok(self):
        # Mutasi DANA tanpa nama pengirim, nomor VA di keterangan = HP panel → cocok.
        p = self._panel(-126000, "Angger Praja", "DANA|Angger Praja|082264436674",
                        "p1", datetime(2026, 7, 1, 10, 0))
        b = self._bank(-126000,
                       "TRSF E-BANKING DB 0107/FTFVA/WS9501139010/DANA - - 82264436674",
                       "b1", datetime(2026, 7, 1, 11, 0))
        run = run_match("panel_bank", self.tol)
        r = MatchResult.objects.get(run=run, left=p)
        self.assertEqual(r.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(r.right, b)
        self.assertEqual(r.score, 100)

    def test_gopay_topup_nomor_cocok(self):
        p = self._panel(-51000, "Bayu Hartono", "GOPAY|Bayu Hartono|0895389905759",
                        "p1", datetime(2026, 7, 1, 10, 0))
        b = self._bank(-51000,
                       "TRSF E-BANKING DB 0107/FTFVA/WS9501170001/GO-PAY TOPUP- - 0895389905759",
                       "b1", datetime(2026, 7, 1, 11, 0))
        run = run_match("panel_bank", self.tol)
        r = MatchResult.objects.get(run=run, left=p)
        self.assertEqual(r.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(r.right, b)

    def test_nomor_beda_tidak_dipasangkan(self):
        # Nomor HP beda + tak ada nama di mutasi → tidak boleh dipasangkan blind.
        p = self._panel(-70000, "Hermanto", "DANA|Hermanto|087886695145",
                        "p1", datetime(2026, 7, 1, 22, 50))
        self._bank(-70000,
                   "TRSF E-BANKING DB 0107/FTFVA/WS9501139010/DANA - - 81328680665",
                   "b1", datetime(2026, 7, 1, 0, 0))
        run = run_match("panel_bank", self.tol)
        r = MatchResult.objects.get(run=run, left=p)
        self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)
        self.assertEqual(r.reason_code, "no_money")
        self.assertIsNone(r.right)


class BrivaEWalletTests(TestCase):
    """WD e-wallet via BRI: mutasi berpola 'BRIVA<kode kanal><no HP>' (kode 5/3
    digit menempel di depan nomor → deret 16-18 digit, lolos dari regex deret
    9-15). Kode dari matriks klien: DANA 88810, GOPAY 30135, OVO 88099,
    SHOPEEPAY 112, LINK AJA 91188. Kasus prod WLG Batch #6 (10-Jul-2026):
    15 WD GOPAY/LINKAJA jatuh 'identitas beda' padahal mutasinya ada."""

    def setUp(self):
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bank = SourceType.objects.get_or_create(
            key="bank", defaults={"name": "Bank", "is_money_source": True}
        )[0]
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1, "fuzzy_threshold": 85}
        )[0]
        self.up = Upload.objects.create(source_type=self.panel)
        self.upb = Upload.objects.create(source_type=self.bank)

    def _panel(self, money, name, pbank, rh, dt):
        return Transaction.objects.create(
            upload=self.up, source_type=self.panel, jenis="wd",
            amount=Decimal(abs(money)), money_delta=Decimal(money),
            counterparty=name, occurred_at=dt, row_hash=rh,
            raw={"Player Bank": pbank},
        )

    def _bank(self, money, desc, rh, dt):
        return Transaction.objects.create(
            upload=self.upb, source_type=self.bank, jenis="wd",
            amount=Decimal(abs(money)), money_delta=Decimal(money),
            counterparty="", occurred_at=dt, row_hash=rh,
            description=desc, raw={"DESK_TRAN": desc},
        )

    DESC_GOPAY = ("BRIVA30135083144889247NBMBAxxxx Pxxxx "
                  "BRIVA 30135083144889247NBMBAxxxx ESB:NBMB:0200200P:174837810133")

    def test_gopay_briva_nomor_cocok(self):
        # Kasus riil: turufc/Ariyo padli 70rb — HP di balik kode GOPAY 30135.
        p = self._panel(-70000, "Ariyo padli", "GOPAY|Ariyo padli|083144889247",
                        "p1", datetime(2026, 7, 10, 23, 41))
        b = self._bank(-70000, self.DESC_GOPAY, "b1", datetime(2026, 7, 10, 23, 44))
        run = run_match("panel_bank", self.tol)
        r = MatchResult.objects.get(run=run, left=p)
        self.assertEqual(r.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(r.right, b)
        self.assertEqual(r.score, 100)

    def test_linkaja_briva_nomor_cocok(self):
        p = self._panel(-100000, "Sari Dewi", "LINK AJA|Sari Dewi|083147803695",
                        "p1", datetime(2026, 7, 10, 20, 0))
        b = self._bank(-100000,
                       "BRIVA91188083147803695NBMBAxxxx Pxxxx "
                       "BRIVA 91188083147803695NBMBAxxxx ESB:NBMB:0200200P:174837810188",
                       "b1", datetime(2026, 7, 10, 20, 5))
        run = run_match("panel_bank", self.tol)
        r = MatchResult.objects.get(run=run, left=p)
        self.assertEqual(r.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(r.right, b)

    def test_baris_fee_seribu_tidak_terpakai(self):
        # Tiap transfer BRIVA berpasangan baris fee Rp1.000 berdeskripsi IDENTIK
        # (SEQ sama). Blocking nominal harus menjodohkan ke transfernya, bukan fee.
        p = self._panel(-70000, "Ariyo padli", "GOPAY|Ariyo padli|083144889247",
                        "p1", datetime(2026, 7, 10, 23, 41))
        b = self._bank(-70000, self.DESC_GOPAY, "b1", datetime(2026, 7, 10, 23, 44))
        fee = self._bank(-1000, self.DESC_GOPAY, "b2", datetime(2026, 7, 10, 23, 44))
        run = run_match("panel_bank", self.tol)
        r = MatchResult.objects.get(run=run, left=p)
        self.assertEqual(r.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(r.right, b)
        self.assertFalse(MatchResult.objects.filter(run=run, right=fee).exists())


class BrivaPhoneExtractionTests(TestCase):
    """Unit _money_phones utk pola BRIVA — hardening temuan review adversarial."""

    def _phones(self, desc):
        from types import SimpleNamespace
        from reconciliation.engine import _money_phones
        return _money_phones(SimpleNamespace(raw={"DESK_TRAN": desc}, counterparty=""))

    def test_huruf_kecil_dan_spasi_ganda_tetap_terbaca(self):
        for desc in ("briva30135083144889247NBMB",
                     "BRIVA  30135083144889247NBMB"):
            self.assertIn("83144889247", self._phones(desc), desc)

    def test_deret_tercemar_kode_ikut_terbuang(self):
        # '30135083144889247' TIDAK boleh menyisakan '301350831448892' sebagai
        # anchor — rekening BRI 15 digit berawalan 30135 bisa salah nyangkut.
        phones = self._phones("BRIVA30135083144889247NBMBAxxxx")
        self.assertIn("83144889247", phones)
        self.assertNotIn("301350831448892", phones)

    def test_deret_lain_di_teks_tetap_diambil(self):
        # Referensi ESB dsb. tetap ikut (perilaku lama, di luar span BRIVA).
        phones = self._phones("BRIVA30135083144889247 ESB:NBMB:0200200P:174837810133")
        self.assertIn("174837810133", phones)


class MoneyPhonesAngkaUangTests(TestCase):
    """W1-8: _money_phones tidak boleh menelan angka UANG — saldo/nominal
    ≥ Rp100jt menghasilkan deret ≥9 digit yang lolos jadi 'nomor HP' skor 100."""

    def _phones(self, raw, cp=""):
        from types import SimpleNamespace

        from reconciliation.engine import _money_phones
        return _money_phones(SimpleNamespace(raw=raw, counterparty=cp))

    def test_key_saldo_tidak_ikut_discan(self):
        phones = self._phones(
            {"Saldo": "8123456789.00", "Keterangan": "TRSF DANA 081298765432"}
        )
        self.assertEqual(phones, {"81298765432"})

    def test_angka_desimal_uang_di_teks_bebas_dibuang(self):
        phones = self._phones(
            {"Keterangan": "bayar 8123456789.00 utk 081298765432"}
        )
        self.assertEqual(phones, {"81298765432"})

    def test_variasi_key_uang_terblokir(self):
        for key in ("Balance", "AMOUNT", "nominal_tx", "Jumlah", "credit",
                    "Debit", "MutasiDebet", "fee_admin", "Total", "harga"):
            phones = self._phones({key: "9876543210", "ket": "HP 081298765432"})
            self.assertEqual(phones, {"81298765432"}, key)

    def test_counterparty_tetap_ikut(self):
        phones = self._phones({}, cp="DANA 081298765432")
        self.assertEqual(phones, {"81298765432"})
