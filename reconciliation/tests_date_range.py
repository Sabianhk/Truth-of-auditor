"""W4-1 — filter tanggal sargable: batas hari harus PERSIS sama dengan
semantik lama `occurred_at__date__gte/lte` (baris 00:00 dan 23:59 masuk,
hari berikutnya tidak)."""
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase

from reconciliation.engine import _date_filter, _date_range_q
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction


class DateRangeSargableTests(TestCase):
    def setUp(self):
        self.toko = Toko.objects.get(key="lbs")
        self.panel = SourceType.objects.get_or_create(
            key="panel", defaults={"name": "Panel"}
        )[0]
        self.up = Upload.objects.create(source_type=self.panel, toko=self.toko)

    def _tx(self, rh, dt):
        return Transaction.objects.create(
            upload=self.up, source_type=self.panel, toko=self.toko, jenis="depo",
            amount=Decimal("1000"), money_delta=Decimal("1000"),
            occurred_at=dt, row_hash=rh,
        )

    def test_batas_hari_inklusif(self):
        awal = self._tx("h00", datetime(2026, 6, 27, 0, 0, 0))
        akhir = self._tx("h2359", datetime(2026, 6, 27, 23, 59, 59))
        besok = self._tx("besok", datetime(2026, 6, 28, 0, 0, 0))
        kemarin = self._tx("kemarin", datetime(2026, 6, 26, 23, 59, 59))
        d = date(2026, 6, 27)

        qs = _date_filter(Transaction.objects.all(), d, d)
        ids = set(qs.values_list("id", flat=True))
        self.assertIn(awal.id, ids)
        self.assertIn(akhir.id, ids)
        self.assertNotIn(besok.id, ids)
        self.assertNotIn(kemarin.id, ids)

    def test_setengah_terbuka(self):
        t = self._tx("t", datetime(2026, 6, 27, 12, 0))
        # hanya dfrom → tanpa batas atas; hanya dto → tanpa batas bawah
        self.assertIn(
            t.id,
            _date_filter(Transaction.objects.all(), date(2026, 6, 27), None)
            .values_list("id", flat=True),
        )
        self.assertIn(
            t.id,
            _date_filter(Transaction.objects.all(), None, date(2026, 6, 27))
            .values_list("id", flat=True),
        )
        self.assertNotIn(
            t.id,
            _date_filter(Transaction.objects.all(), date(2026, 6, 28), None)
            .values_list("id", flat=True),
        )
        self.assertNotIn(
            t.id,
            _date_filter(Transaction.objects.all(), None, date(2026, 6, 26))
            .values_list("id", flat=True),
        )

    def test_koersi_string_iso(self):
        # CLI `match --from/--to` mengirim string — harus dikoersi, bukan meledak.
        t = self._tx("s", datetime(2026, 6, 27, 8, 0))
        qs = Transaction.objects.filter(_date_range_q("occurred_at", "2026-06-27", "2026-06-27"))
        self.assertIn(t.id, qs.values_list("id", flat=True))

    def test_null_occurred_at_tetap_tersaring(self):
        # NULL tak lolos filter rentang — sama dgn semantik __date__ lama.
        t = self._tx("null", None)
        qs = _date_filter(Transaction.objects.all(), date(2026, 6, 27), date(2026, 6, 27))
        self.assertNotIn(t.id, qs.values_list("id", flat=True))
