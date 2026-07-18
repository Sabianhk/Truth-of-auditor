"""Rekonsiliasi otomatis per tanggal (auto-split): satu batch per tanggal-panel.

Panel jadi jangkar. Loop MENAIK memakai carry-over bawaan (run_batch date_from=None,
date_to=D). Pre-flight verify_panel_anchor memblokir uang/bracket tanpa panel penutup.
"""
from datetime import date, datetime
from decimal import Decimal

from django.test import TestCase

from reconciliation.engine import (
    _panel_dates,
    run_batch,
    run_batches_auto,
    verify_panel_anchor,
)
from reconciliation.models import MatchResult, ReconBatch, ToleranceProfile
from sources.models import SourceType, Toko, Upload
from transactions.models import Transaction


class _Base(TestCase):
    def setUp(self):
        self.lbs = Toko.objects.get(key="lbs")
        self.tol = ToleranceProfile.objects.get_or_create(
            name="Default", defaults={"date_window_days": 1}
        )[0]
        self.panel = SourceType.objects.get_or_create(key="panel", defaults={"name": "Panel"})[0]
        self.bracket = SourceType.objects.get_or_create(key="bracket", defaults={"name": "Bracket"})[0]
        self.bank = SourceType.objects.get_or_create(key="bank", defaults={"name": "Bank"})[0]
        self.gateway = SourceType.objects.get_or_create(key="gateway", defaults={"name": "Gateway"})[0]
        self.up = Upload.objects.create(source_type=self.panel, toko=self.lbs)

    def _tx(self, st, jenis, amount, money, ticket, rh, dt, **kw):
        return Transaction.objects.create(
            upload=self.up, source_type=st, toko=self.lbs, jenis=jenis,
            amount=Decimal(amount), money_delta=Decimal(money), ticket_no=ticket,
            occurred_at=dt, row_hash=rh, **kw,
        )

    def _hari(self, st, jenis, amount, money, ticket, rh, hari, jam=10, **kw):
        return self._tx(st, jenis, amount, money, ticket, rh,
                        datetime(2026, 6, hari, jam, 0), **kw)


class AutoSplitTests(_Base):
    def test_tiga_tanggal_jadi_tiga_batch(self):
        # Tiap tanggal punya panel + uang sehari (cocok). 3 tanggal → 3 batch.
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        self._hari(self.panel, "depo", "60000", "60000", "D2", "p2", 28, username="andi")
        self._hari(self.bank, "depo", "60000", "60000", "", "k2", 28, username="andi")
        self._hari(self.panel, "depo", "70000", "70000", "D3", "p3", 29, username="cici")
        self._hari(self.bank, "depo", "70000", "70000", "", "k3", 29, username="cici")

        res = run_batches_auto(self.lbs, self.tol)
        self.assertTrue(res["ok"])
        self.assertEqual([b.recon_date for b in res["batches"]],
                         [date(2026, 6, 27), date(2026, 6, 28), date(2026, 6, 29)])
        self.assertEqual(ReconBatch.objects.filter(recon_date__isnull=False).count(), 3)
        for b in res["batches"]:
            self.assertEqual(b.summary["buckets"]["cocok"], 1)

    def test_satu_tanggal_tetap_satu_batch(self):
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        res = run_batches_auto(self.lbs, self.tol)
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["batches"]), 1)
        self.assertEqual(res["batches"][0].recon_date, date(2026, 6, 27))

    def test_carry_over_lintas_hari_dalam_auto_run(self):
        # Panel 27 (50k budi) uangnya baru datang 28; day-27 punya bank 70k (siti)
        # supaya PANEL_BANK jalan → panel27 no_money → carried. Panel 28 murni.
        p27 = self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, jam=21, username="budi")
        self._hari(self.bank, "depo", "70000", "70000", "", "k1", 27, username="siti")
        self._hari(self.panel, "depo", "60000", "60000", "D2", "p2", 28, jam=9, username="andi")
        self._hari(self.bank, "depo", "60000", "60000", "", "k2", 28, username="andi")
        uang = self._hari(self.bank, "depo", "50000", "50000", "", "k3", 28, jam=1, username="budi")

        res = run_batches_auto(self.lbs, self.tol)
        self.assertTrue(res["ok"])
        b27, b28 = res["batches"]
        self.assertEqual(b27.recon_date, date(2026, 6, 27))
        self.assertEqual(b28.recon_date, date(2026, 6, 28))
        # Settle terlambat: hasil panel27 ter-flip jadi COCOK di batch ASALnya (b27).
        r = MatchResult.objects.get(run__batch=b27, left=p27)
        self.assertEqual(r.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(r.reason_code, "late_settlement")
        self.assertEqual(r.resolved_by_batch, b28)
        self.assertEqual(r.right, uang)
        p27.refresh_from_db()
        self.assertEqual(p27.consumed_by_batch, b27)  # pulang ke batch asal
        # Batch 28 murni tanggal 28 — nilai carried tidak ikut gross.
        self.assertEqual(b28.summary["dp"]["panel"], 60000.0)
        self.assertEqual(b28.summary["late_settlement"]["dp"], {"count": 1, "amount": 50000.0})

    def test_tanggal_sudah_ada_batch_dilewati(self):
        # Batch 27 sudah ada (dari run manual). Lalu ada panel-27 SUSULAN aktif +
        # data tanggal 28. Auto-run melewati 27 (dilaporkan), hanya bikin batch 28.
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        b27 = run_batch(self.lbs, self.tol, recon_date=date(2026, 6, 27))
        # Panel 27 susulan (aktif) + tanggal 28.
        self._hari(self.panel, "depo", "80000", "80000", "D9", "p9", 27, username="rian")
        self._hari(self.panel, "depo", "60000", "60000", "D2", "p2", 28, username="andi")
        self._hari(self.bank, "depo", "60000", "60000", "", "k2", 28, username="andi")

        res = run_batches_auto(self.lbs, self.tol)
        self.assertTrue(res["ok"])
        self.assertEqual([b.recon_date for b in res["batches"]], [date(2026, 6, 28)])
        self.assertEqual([s["date"] for s in res["skipped_existing"]], [date(2026, 6, 27)])
        self.assertEqual(res["skipped_existing"][0]["batch_id"], b27.id)
        self.assertEqual(res["errors"], [])


    def test_deposit_tanpa_uang_sehari_tidak_hilang_senyap(self):
        # Jaring senyap (fix bug D): panel 27 yang uangnya hanya datang 28 (tanpa
        # panel 28) TIDAK boleh dikonsumsi diam-diam — harus tercatat no_money
        # supaya selisih batch selalu punya baris penjelas.
        p27 = self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, jam=21, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 28, jam=1, username="budi")
        res = run_batches_auto(self.lbs, self.tol)
        self.assertTrue(res["ok"])
        r = MatchResult.objects.filter(left=p27).first()
        self.assertIsNotNone(r, "deposit hilang senyap — tak ada MatchResult")
        self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)
        self.assertEqual(r.reason_code, "no_money")


class VerifyAnchorTests(_Base):
    def test_uang_dalam_rentang_tanpa_panel_memblokir(self):
        # Panel 27 & 30 (rentang 27-30); uang 29 DALAM rentang tanpa panel penutup
        # (window 1) → tolak, 0 batch.
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        self._hari(self.panel, "depo", "80000", "80000", "D2", "p2", 30, username="andi")
        self._hari(self.bank, "depo", "80000", "80000", "", "k2", 30, username="andi")
        self._hari(self.bank, "depo", "99000", "99000", "", "k9", 29, username="zola")
        res = run_batches_auto(self.lbs, self.tol)
        self.assertFalse(res["ok"])
        self.assertIn((date(2026, 6, 29), "uang"),
                      [(v["date"], v["source"]) for v in res["violations"]])
        self.assertEqual(ReconBatch.objects.filter(recon_date__isnull=False).count(), 0)

    def test_uang_sebelum_panel_tak_memblokir_tak_dikonsumsi(self):
        # Statement bank sebulan penuh: uang 1/10/20 SEBELUM panel (27). Tak memblokir,
        # dan uang itu TAK dikonsumsi (menunggu panel tanggalnya diupload).
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        early = [self._hari(self.bank, "depo", "99000", "99000", "", f"b{d}", d) for d in (1, 10, 20)]
        res = run_batches_auto(self.lbs, self.tol)
        self.assertTrue(res["ok"], res["violations"])
        self.assertEqual([b.recon_date for b in res["batches"]], [date(2026, 6, 27)])
        for e in early:
            e.refresh_from_db()
            self.assertIsNone(e.consumed_by_batch)  # tetap aktif, menunggu

    def test_uang_setelah_rentang_panel_tak_memblokir(self):
        # Uang 30 setelah panel 27 (di luar window) → di luar rentang → bukan pelanggaran.
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        self._hari(self.bank, "depo", "90000", "90000", "", "k2", 30, username="siti")
        self.assertEqual(verify_panel_anchor(self.lbs, None, None, None, 1), [])

    def test_uang_dalam_window_lolos(self):
        # Panel 27, uang 28 (window 1: 27<=28<=28) → tertutup, lolos.
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 28, username="budi")
        self.assertEqual(verify_panel_anchor(self.lbs, None, None, None, 1), [])

    def test_tanpa_panel_tak_ada_pelanggaran(self):
        # Hanya uang, tak ada panel → tak ada yang direkon → tak memblokir.
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        self.assertEqual(verify_panel_anchor(self.lbs, None, None, None, 1), [])

    def test_admin_fee_tanpa_panel_tidak_memblokir(self):
        # Baris admin (fee) di tanggal tanpa panel tidak memicu pelanggaran.
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        self._hari(self.bank, "admin", "6500", "-6500", "", "k9", 28)
        self.assertEqual(verify_panel_anchor(self.lbs, None, None, None, 1), [])

    def test_bracket_dalam_rentang_yatim_memblokir(self):
        # Panel 26 & 30 (rentang); bracket 28 DALAM rentang, |28-26|=2 & |28-30|=2 >
        # window 1 → pelanggaran.
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 26, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 26, username="budi")
        self._hari(self.panel, "depo", "70000", "70000", "D2", "p2", 30, username="andi")
        self._hari(self.bank, "depo", "70000", "70000", "", "k2", 30, username="andi")
        self._hari(self.bracket, "depo", "40000", "40000", "D5", "b5", 28, username="tono")
        res = run_batches_auto(self.lbs, self.tol)
        self.assertFalse(res["ok"])
        self.assertIn("bracket", [v["source"] for v in res["violations"]])

    def test_carry_lintas_run_terpisah(self):
        # Carry-over lintas RUN terpisah (bukan satu auto-run): scope lo tak boleh
        # mengeluarkan baris carried lama. Hari 27 → carried; hari 28 run lagi → settle.
        p27 = self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, jam=21, username="budi")
        self._hari(self.bank, "depo", "70000", "70000", "", "k1", 27, username="siti")
        run_batches_auto(self.lbs, self.tol)  # run hari 27
        self._hari(self.panel, "depo", "60000", "60000", "D2", "p2", 28, jam=9, username="andi")
        self._hari(self.bank, "depo", "60000", "60000", "", "k2", 28, username="andi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k3", 28, jam=1, username="budi")
        run_batches_auto(self.lbs, self.tol)  # run hari 28 → settle carried 27
        r = MatchResult.objects.get(left=p27)
        self.assertEqual(r.reason_code, "late_settlement")

    def test_panel_dates_menaik_dan_aktif(self):
        self._hari(self.panel, "depo", "60000", "60000", "D2", "p2", 29, username="andi")
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self.assertEqual(_panel_dates(self.lbs), [date(2026, 6, 27), date(2026, 6, 29)])


class AutoSplitStopOnErrorTests(_Base):
    """W1-3: begitu satu tanggal gagal, loop auto-split BERHENTI — tanggal
    berikutnya bisa mengonsumsi baris milik tanggal gagal (home batch-nya tak
    ada), jadi tidak boleh diproses."""

    def _dua_tanggal(self):
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        self._hari(self.panel, "depo", "60000", "60000", "D2", "p2", 28, username="andi")
        self._hari(self.bank, "depo", "60000", "60000", "", "k2", 28, username="andi")

    def test_tanggal_gagal_menghentikan_loop(self):
        self._dua_tanggal()
        from unittest import mock

        from reconciliation import engine

        calls = []

        def boom(toko, tolerance, **kw):
            calls.append(kw.get("recon_date"))
            raise RuntimeError("boom")

        with mock.patch.object(engine, "run_batch", side_effect=boom):
            with self.assertLogs("reconciliation.engine", level="ERROR"):
                res = run_batches_auto(self.lbs, self.tol)
        # Hanya tanggal PERTAMA yang dicoba; error dilaporkan; tanggal kedua
        # tidak diproses dan tidak punya batch.
        self.assertEqual(calls, [date(2026, 6, 27)])
        self.assertEqual([e["date"] for e in res["errors"]], [date(2026, 6, 27)])
        self.assertEqual(res["batches"], [])
        self.assertFalse(
            ReconBatch.objects.filter(toko=self.lbs, recon_date=date(2026, 6, 28)).exists()
        )

    def test_tanpa_error_kedua_tanggal_diproses(self):
        self._dua_tanggal()
        res = run_batches_auto(self.lbs, self.tol)
        self.assertEqual(res["errors"], [])
        self.assertEqual([b.recon_date for b in res["batches"]],
                         [date(2026, 6, 27), date(2026, 6, 28)])


class ConsumeFloorTests(_Base):
    """W1-2: celah lo-widening — run_batches_auto melebarkan date_from ke tanggal
    baris carried terawal; uang TAK BERPASANGAN di celah [lo..tanggal panel batch)
    yang panelnya belum diupload tidak boleh ikut terkonsumsi tanpa MatchResult
    (harus tetap aktif menunggu panel tanggalnya). Uang di celah yang BERPASANGAN
    (settle carried) tetap dikonsumsi seperti biasa."""

    def _skenario(self):
        """Batch 22 (window 2) meninggalkan carried panel-22. Lalu muncul:
        uang 23 (settle si carried), uang 24 ORPHAN (panelnya belum ada),
        dan panel+uang 27. Auto-run memproses tanggal 27 dengan lo=22."""
        longgar2 = ToleranceProfile.objects.get_or_create(
            name="Longgar2", defaults={"date_window_days": 2}
        )[0]
        p22 = self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 22,
                         jam=21, username="budi")
        self._hari(self.bank, "depo", "70000", "70000", "", "k0", 22, username="siti")
        b22 = run_batch(self.lbs, longgar2, recon_date=date(2026, 6, 22))
        k23 = self._hari(self.bank, "depo", "50000", "50000", "", "k23", 23,
                         jam=1, username="budi")   # settle carried p22
        k24 = self._hari(self.bank, "depo", "90000", "90000", "", "k24", 24,
                         username="rudi")          # orphan — panel 24 belum diupload
        self._hari(self.panel, "depo", "60000", "60000", "D2", "p27", 27, username="andi")
        self._hari(self.bank, "depo", "60000", "60000", "", "k27", 27, username="andi")
        return longgar2, p22, b22, k23, k24

    def test_uang_orphan_di_celah_tidak_dikonsumsi(self):
        longgar2, p22, b22, k23, k24 = self._skenario()
        res = run_batches_auto(self.lbs, longgar2)
        self.assertTrue(res["ok"], res["violations"])
        b27 = res["batches"][0]
        # Carried p22 settle oleh uang 23 → uangnya TETAP dikonsumsi (berpasangan).
        r = MatchResult.objects.get(run__batch=b22, left=p22)
        self.assertEqual(r.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(r.right, k23)
        k23.refresh_from_db()
        self.assertEqual(k23.consumed_by_batch, b27)
        # Orphan 24: TIDAK dikonsumsi (menunggu panel 24), tanpa MatchResult.
        k24.refresh_from_db()
        self.assertIsNone(k24.consumed_by_batch)
        self.assertFalse(
            MatchResult.objects.filter(right=k24).exists()
        )

    def test_orphan_gap_day_antar_panel_dikonsumsi_dan_terklasifikasi(self):
        """W6-1: uang orphan di HARI TANPA PANEL di antara dua tanggal panel
        (gap-day; hari itu memang tak pernah punya panel) TIDAK dilindungi
        consume_floor — floor = tanggal panel TERAWAL scope, jadi batch tanggal
        berikutnya mengonsumsinya dan mencatatnya sebagai no_panel (kategori d),
        seperti perilaku pra-auto-split. Yang dilindungi hanya uang SEBELUM
        panel terawal (celah lo-widening asli)."""
        longgar2 = ToleranceProfile.objects.get_or_create(
            name="Longgar2", defaults={"date_window_days": 2}
        )[0]
        self._hari(self.panel, "depo", "50000", "50000", "D1", "p1", 27, username="budi")
        self._hari(self.bank, "depo", "50000", "50000", "", "k1", 27, username="budi")
        self._hari(self.panel, "depo", "80000", "80000", "D2", "p2", 30, username="andi")
        self._hari(self.bank, "depo", "80000", "80000", "", "k2", 30, username="andi")
        # Uang nyasar tanggal 28 — hari 28 tak punya panel (dan tak akan punya).
        nyasar = self._hari(self.bank, "depo", "99000", "99000", "", "k9", 28,
                            username="zola")
        res = run_batches_auto(self.lbs, longgar2)
        self.assertTrue(res["ok"], res["violations"])
        b30 = res["batches"][-1]
        self.assertEqual(b30.recon_date, date(2026, 6, 30))
        nyasar.refresh_from_db()
        self.assertEqual(nyasar.consumed_by_batch, b30,
                         "orphan gap-day harus dikonsumsi batch berikutnya")
        r = MatchResult.objects.get(right=nyasar)
        self.assertEqual(r.reason_code, "no_panel")
        self.assertEqual(r.bucket, MatchResult.Bucket.TIDAK)

    def test_orphan_di_celah_sembuh_saat_panelnya_datang(self):
        longgar2, p22, b22, k23, k24 = self._skenario()
        run_batches_auto(self.lbs, longgar2)
        # Panel 24 akhirnya diupload → auto-run berikutnya membuat batch 24
        # dan uang orphan-nya match normal.
        p24 = self._hari(self.panel, "depo", "90000", "90000", "D3", "p24", 24,
                         username="rudi")
        res = run_batches_auto(self.lbs, longgar2)
        self.assertTrue(res["ok"], res["violations"])
        self.assertEqual([b.recon_date for b in res["batches"]], [date(2026, 6, 24)])
        r = MatchResult.objects.get(left=p24)
        self.assertEqual(r.bucket, MatchResult.Bucket.COCOK)
        self.assertEqual(r.right, k24)
        k24.refresh_from_db()
        self.assertEqual(k24.consumed_by_batch, res["batches"][0])
