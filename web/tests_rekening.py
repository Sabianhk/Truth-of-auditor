"""Rincian Rekening — breakdown sisi UANG (bank/gateway) per rekening per hari.

Kembaran Breakdown Bracket untuk mutasi bank nyata: Deposit / Withdraw / Admin
/ Net / Trx / Saldo Awal / Saldo Akhir / Selisih Kontrol per rekening operator.
"""
from datetime import date, datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction
from web.rekening import rekening_breakdown

TGL = date(2026, 6, 28)


class _MoneyData(TestCase):
    def setUp(self):
        self.toko = Toko.objects.get(key="lbs")
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.gw = SourceType.objects.get_or_create(key="gateway", defaults={"name": "Gateway"})[0]
        self._n = 0
        self._uploads = {}

    def _upload(self, st, provider, owner):
        key = (st.id, provider, owner)
        if key not in self._uploads:
            self._uploads[key] = Upload.objects.create(
                source_type=st, toko=self.toko, provider=provider, owner_name=owner,
            )
        return self._uploads[key]

    def mv(self, st, provider, owner, money, saldo, jam=10, jenis="depo", tanggal=TGL):
        """Satu baris mutasi uang. `money` bertanda, `saldo`=balance_after (str|None)."""
        self._n += 1
        return Transaction.objects.create(
            upload=self._upload(st, provider, owner), source_type=st, toko=self.toko,
            jenis=jenis, amount=abs(Decimal(money)), money_delta=Decimal(money),
            balance_after=None if saldo is None else Decimal(saldo),
            occurred_at=datetime(tanggal.year, tanggal.month, tanggal.day, jam, 0),
            row_hash=f"mv{self._n}",
        )


class RekeningAggregatTests(_MoneyData):
    def test_deposit_withdraw_net_saldo_selisih(self):
        # BCA a/n HENDI: awal 1.000.000, +DP 500rb → 1.500.000, −WD 200rb → 1.300.000
        self.mv(self.bank, "BCA", "HENDI", "500000", "1500000", jam=9)
        self.mv(self.bank, "BCA", "HENDI", "-200000", "1300000", jam=11, jenis="wd")
        (acc,) = rekening_breakdown(self.toko, TGL)["accounts"]
        self.assertEqual(acc["label"], "BCA a/n HENDI")
        self.assertEqual(acc["deposit"], Decimal("500000"))
        self.assertEqual(acc["withdraw"], Decimal("200000"))
        self.assertEqual(acc["net"], Decimal("300000"))
        self.assertEqual(acc["trx"], 2)
        self.assertEqual(acc["saldo_awal"], Decimal("1000000"))
        self.assertEqual(acc["saldo_akhir"], Decimal("1300000"))
        self.assertEqual(acc["selisih"], Decimal("0"))

    def test_fee_admin_terpisah_dan_ikut_saldo(self):
        # WD 100rb + fee admin 2.500 → saldo 897.500 dari awal 1.000.000
        self.mv(self.bank, "BCA", "NIJUN", "-100000", "900000", jam=9, jenis="wd")
        self.mv(self.bank, "BCA", "NIJUN", "-2500", "897500", jam=10, jenis="admin")
        (acc,) = rekening_breakdown(self.toko, TGL)["accounts"]
        self.assertEqual(acc["withdraw"], Decimal("100000"))
        self.assertEqual(acc["admin"], Decimal("-2500"))
        self.assertEqual(acc["trx"], 1)  # fee tidak dihitung sebagai transaksi
        self.assertEqual(acc["mutasi"], Decimal("-102500"))
        self.assertEqual(acc["selisih"], Decimal("0"))

    def test_selisih_terdeteksi_saat_saldo_janggal(self):
        self.mv(self.bank, "BRI", "PANCA", "-50000", "150000", jam=9, jenis="wd")
        self.mv(self.bank, "BRI", "PANCA", "-50000", "120000", jam=10, jenis="wd")
        # awal 200rb, mutasi −100rb → seharusnya 100rb, tapi FR-nya 120rb → selisih +20rb
        (acc,) = rekening_breakdown(self.toko, TGL)["accounts"]
        self.assertEqual(acc["selisih"], Decimal("20000"))

    def test_gateway_tanpa_saldo_selisih_none(self):
        self.mv(self.gw, "QRFLYER", "", "351726000", None, jam=8)
        (acc,) = rekening_breakdown(self.toko, TGL)["accounts"]
        self.assertEqual(acc["label"], "QR FLYER")
        self.assertEqual(acc["deposit"], Decimal("351726000"))
        self.assertIsNone(acc["saldo_awal"])
        self.assertIsNone(acc["selisih"])

    def test_urut_bank_dulu_lalu_gateway(self):
        self.mv(self.gw, "NXPAY", "", "1000", "1000", jam=8)
        self.mv(self.bank, "BCA", "HENDI", "1000", "1000", jam=9)
        labels = [a["label"] for a in rekening_breakdown(self.toko, TGL)["accounts"]]
        self.assertEqual(labels[0], "BCA a/n HENDI")
        self.assertEqual(labels[-1], "NXPAY")

    def test_total_lintas_rekening(self):
        self.mv(self.bank, "BCA", "HENDI", "500000", "500000", jam=9)
        self.mv(self.bank, "BRI", "PANCA", "-100000", "900000", jam=9, jenis="wd")
        tot = rekening_breakdown(self.toko, TGL)["total"]
        self.assertEqual(tot["deposit"], Decimal("500000"))
        self.assertEqual(tot["withdraw"], Decimal("100000"))
        self.assertEqual(tot["net"], Decimal("400000"))
        self.assertEqual(tot["trx"], 2)

    def test_tanggal_lain_tak_ikut(self):
        self.mv(self.bank, "BCA", "HENDI", "500000", "500000", jam=9)
        self.mv(self.bank, "BCA", "HENDI", "999", "999", jam=9, tanggal=date(2026, 6, 27))
        data = rekening_breakdown(self.toko, TGL)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["accounts"][0]["deposit"], Decimal("500000"))


class RekeningPerAccountTests(_MoneyData):
    """W3-5: group per rekening NYATA — (label, account efektif), bukan label
    saja. Dua rekening BCA pemilik sama (label identik) tidak boleh digabung
    satu rantai saldo (saldo awal/akhir/kontrol jadi salah)."""

    def _mv_acc(self, account_no, money, saldo, jam):
        from sources.models import Account

        acc = Account.objects.create(
            kind="bank", provider="BCA", name="BCA HENDI",
            account_no=account_no, toko=self.toko,
        )
        up = Upload.objects.create(
            source_type=self.bank, toko=self.toko, provider="BCA",
            owner_name="HENDI", account=acc,
        )
        self._n += 1
        return Transaction.objects.create(
            upload=up, source_type=self.bank, toko=self.toko, jenis="depo",
            amount=abs(Decimal(money)), money_delta=Decimal(money),
            balance_after=Decimal(saldo),
            occurred_at=datetime(TGL.year, TGL.month, TGL.day, jam, 0),
            row_hash=f"acc{self._n}",
        )

    def test_dua_rekening_label_sama_jadi_dua_baris(self):
        self._mv_acc("1234567890", "500000", "1500000", 9)   # rek A: awal 1.000.000
        self._mv_acc("9876543210", "200000", "700000", 10)   # rek B: awal 500.000
        data = rekening_breakdown(self.toko, TGL)
        self.assertEqual(len(data["accounts"]), 2,
                         "dua rekening nyata tidak boleh digabung satu rantai saldo")
        labels = sorted(a["label"] for a in data["accounts"])
        self.assertEqual(labels, ["BCA a/n HENDI — 1234567890",
                                  "BCA a/n HENDI — 9876543210"])
        for a in data["accounts"]:
            self.assertEqual(a["selisih"], Decimal("0"), a["label"])

    def test_satu_rekening_label_tetap_polos(self):
        # Tanpa tabrakan label, tampilan tidak berubah (tanpa suffix nomor).
        self._mv_acc("1234567890", "500000", "1500000", 9)
        (acc,) = rekening_breakdown(self.toko, TGL)["accounts"]
        self.assertEqual(acc["label"], "BCA a/n HENDI")


class RekeningViewTests(_MoneyData):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        User.objects.create_user("aud", "a@a.co", "pw12345", role="supervisor")
        self.client.login(username="aud", password="pw12345")
        self.client.post(reverse("set_toko"), {"toko_id": self.toko.id})

    def test_butuh_login(self):
        self.client.logout()
        r = self.client.get(reverse("rekening_breakdown"))
        self.assertEqual(r.status_code, 302)

    def test_render_data(self):
        self.mv(self.bank, "BCA", "HENDI", "500000", "500000", jam=9)
        r = self.client.get(reverse("rekening_breakdown"), {"date": "2026-06-28"})
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("BCA a/n HENDI", html)
        self.assertIn("Rincian Rekening", html)

    def test_empty_state(self):
        r = self.client.get(reverse("rekening_breakdown"), {"date": "2026-06-28"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Belum ada mutasi", r.content.decode())
