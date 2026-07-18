"""W4-3 — recent_panel_tickets terbatas margin + _operator_names dedup.

Set ticket panel utk kategori b tidak lagi memuat SEMUA sejarah toko: dibatasi
occurred_at >= recon_date - (window + 30 hari). Konsekuensi yang memang
diniatkan: uang gateway yatim ber-ticket panel PURBA (> margin) kini tampil
sebagai kategori b (ticket asing) — dulu diam sebagai c/d padahal ticket
purba itu jelas bukan pasangan uang dalam window.
"""
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase

from reconciliation.engine import (
    _operator_names,
    classify_unmatched_money,
    recent_panel_tickets,
)
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction


class TicketMarginTests(TestCase):
    def setUp(self):
        self.toko = Toko.objects.get(key="lbs")
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.gw = SourceType.objects.get_or_create(key="gateway", defaults={"name": "Gateway"})[0]
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.up = Upload.objects.create(source_type=self.panel, toko=self.toko)
        self.recon = date(2026, 6, 27)
        self.window = 1

    def _tx(self, st, ticket, dt, rh, **kw):
        return Transaction.objects.create(
            upload=self.up, source_type=st, toko=self.toko, jenis="depo",
            amount=Decimal("10000"), money_delta=Decimal("10000"),
            ticket_no=ticket, occurred_at=dt, row_hash=rh, **kw,
        )

    def test_ticket_dalam_margin_masuk_purba_tidak(self):
        self._tx(self.panel, "DBARU", datetime(2026, 6, 26, 10, 0), "p-baru")
        # batas: recon - (1 + 30) = 2026-05-27; 00:00 hari batas masih masuk
        self._tx(self.panel, "DBATAS", datetime(2026, 5, 27, 0, 0), "p-batas")
        self._tx(self.panel, "DPURBA", datetime(2026, 5, 26, 23, 59), "p-purba")
        tickets = recent_panel_tickets(self.toko, self.recon, self.window)
        self.assertIn("DBARU", tickets)
        self.assertIn("DBATAS", tickets)
        self.assertNotIn("DPURBA", tickets)

    def test_tanpa_recon_date_tanpa_batas(self):
        self._tx(self.panel, "DPURBA", datetime(2020, 1, 1, 9, 0), "p-purba")
        self.assertIn("DPURBA", recent_panel_tickets(self.toko, None, self.window))

    def test_gateway_ticket_purba_kini_kategori_b(self):
        # Panel purba (> margin) ber-ticket sama TIDAK lagi memblok kategori b:
        # uang gateway dalam window dgn ticket itu = ticket asing (b), bukan d.
        self._tx(self.panel, "D999", datetime(2026, 5, 1, 9, 0), "p-999")
        uang = self._tx(self.gw, "D999", datetime(2026, 6, 27, 9, 0), "g-999",
                        counterparty="QRIS")
        tickets = recent_panel_tickets(self.toko, self.recon, self.window)
        self.assertEqual(
            classify_unmatched_money(uang, self.recon, self.window, tickets, []),
            "b",
        )

    def test_gateway_ticket_dalam_margin_bukan_b(self):
        self._tx(self.panel, "D888", datetime(2026, 6, 25, 9, 0), "p-888")
        uang = self._tx(self.gw, "D888", datetime(2026, 6, 27, 9, 0), "g-888",
                        counterparty="TANPA PENJELASAN")
        tickets = recent_panel_tickets(self.toko, self.recon, self.window)
        self.assertEqual(
            classify_unmatched_money(uang, self.recon, self.window, tickets, []),
            "d",
        )

    def test_operator_names_dedup_tanpa_kosong(self):
        for i in range(3):
            Upload.objects.create(
                source_type=self.bank, toko=self.toko,
                original_name="BCA HENDI 2026.xlsx",
            )
        Upload.objects.create(source_type=self.bank, toko=self.toko, original_name="")
        names = _operator_names(self.toko)
        self.assertEqual(len(names), len(set(names)))  # dedup
        self.assertNotIn("", names)
        self.assertEqual(sorted(names), names)  # deterministik
