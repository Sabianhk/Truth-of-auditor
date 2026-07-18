"""Produksi (DEBUG=False) tanpa env SECRET_KEY harus GAGAL CEPAT — jangan diam-diam
jalan memakai kunci default yang ter-commit di repo."""
import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent


def _import_settings(extra_env):
    """Import truth_auditor.settings di subprocess bersih; return CompletedProcess."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("SECRET_KEY", "DEBUG", "DATABASE_URL",
                        "RAILWAY_ENVIRONMENT", "DEBUG_WITH_PROD_DB")}
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", "import truth_auditor.settings"],
        env=env, capture_output=True, text=True, cwd=BASE_DIR, timeout=60,
    )


class SecretKeyGuardTests(SimpleTestCase):
    def test_prod_tanpa_secret_key_gagal_cepat(self):
        p = _import_settings({"DEBUG": "False"})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("SECRET_KEY", p.stderr)

    def test_prod_dengan_secret_key_jalan(self):
        p = _import_settings({"DEBUG": "False", "SECRET_KEY": "x" * 60})
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_dev_tanpa_secret_key_tetap_jalan(self):
        p = _import_settings({})  # DEBUG default True — fallback dev diperbolehkan
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_railway_tanpa_debug_dianggap_produksi(self):
        # Lingkungan Railway (RAILWAY_ENVIRONMENT ada) tanpa env DEBUG: default
        # harus PRODUKSI (DEBUG=False) → guard SECRET_KEY tetap menyala.
        p = _import_settings({"RAILWAY_ENVIRONMENT": "production"})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("SECRET_KEY", p.stderr)

    def test_railway_dengan_secret_key_dan_db_jalan(self):
        # Railway lengkap (SECRET_KEY + DATABASE_URL) harus boot normal.
        p = _import_settings({
            "RAILWAY_ENVIRONMENT": "production", "SECRET_KEY": "x" * 60,
            "DATABASE_URL": "postgres://u:p@db.internal:5432/railway",
        })
        self.assertEqual(p.returncode, 0, p.stderr)


class DebugProdDbGuardTests(SimpleTestCase):
    """DEBUG=True di atas database produksi = traceback + query bocor ke
    browser SIAPA PUN — kombinasi ini harus mati saat boot, bukan diam-diam jalan."""

    _DB = "postgres://u:p@db.internal:5432/railway"

    def test_debug_dengan_database_url_gagal(self):
        p = _import_settings({"DEBUG": "True", "DATABASE_URL": self._DB})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("DEBUG", p.stderr)

    def test_debug_default_true_dengan_database_url_juga_gagal(self):
        # Tanpa env DEBUG (default lokal True) tapi DATABASE_URL nyantol → sama bahayanya.
        p = _import_settings({"DATABASE_URL": self._DB})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("DEBUG", p.stderr)

    def test_escape_hatch_sadar_membolehkan(self):
        p = _import_settings({
            "DEBUG": "True", "DATABASE_URL": self._DB, "DEBUG_WITH_PROD_DB": "1",
        })
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_prod_debug_false_dengan_database_url_jalan(self):
        p = _import_settings({
            "DEBUG": "False", "SECRET_KEY": "x" * 60, "DATABASE_URL": self._DB,
        })
        self.assertEqual(p.returncode, 0, p.stderr)


class RailwayTanpaDbGuardTests(SimpleTestCase):
    """Railway tanpa DATABASE_URL = fallback sqlite senyap di filesystem
    EPHEMERAL — semua data lenyap tiap redeploy. Harus fail-hard saat boot."""

    def test_railway_tanpa_database_url_gagal(self):
        p = _import_settings({"RAILWAY_ENVIRONMENT": "production", "SECRET_KEY": "x" * 60})
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("DATABASE_URL", p.stderr)

    def test_lokal_tanpa_database_url_tetap_sqlite(self):
        p = _import_settings({})  # dev lokal tanpa env apa pun harus tetap jalan
        self.assertEqual(p.returncode, 0, p.stderr)
