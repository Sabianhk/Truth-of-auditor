"""Perintah `match` wajib --toko: tanpa scope toko, run_match memasangkan
transaksi LINTAS toko (kebocoran isolasi data per-toko)."""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from sources.models import Toko


class _FakeRun:
    pk = 1
    summary = {"cocok": 0, "perlu_tinjau": 0, "tidak_cocok": 0, "left": 0, "right": 0}


class MatchCliTokoTests(TestCase):
    def setUp(self):
        self.toko = Toko.objects.filter(is_active=True).first()

    def test_tanpa_toko_command_error(self):
        with self.assertRaises(CommandError):
            call_command("match", "panel_bank", stdout=StringIO())

    def test_toko_tak_dikenal_command_error(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("match", "panel_bank", toko="toko-fiktif-xyz", stdout=StringIO())
        self.assertIn("toko-fiktif-xyz", str(ctx.exception))

    def test_toko_diteruskan_ke_run_match(self):
        with patch("reconciliation.management.commands.match.run_match",
                   return_value=_FakeRun()) as m:
            call_command("match", "panel_bank", toko=self.toko.name, stdout=StringIO())
        self.assertEqual(m.call_args.kwargs.get("toko"), self.toko)

    def test_resolve_via_key_dan_pk(self):
        with patch("reconciliation.management.commands.match.run_match",
                   return_value=_FakeRun()) as m:
            call_command("match", "panel_bank", toko=self.toko.key, stdout=StringIO())
            call_command("match", "panel_bank", toko=str(self.toko.pk), stdout=StringIO())
        for call in m.call_args_list:
            self.assertEqual(call.kwargs.get("toko"), self.toko)
