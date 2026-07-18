from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse


class HealthzTests(TestCase):
    """Healthcheck deploy Railway: tanpa auth, cek DB hidup."""

    def test_tanpa_login_200_ok(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content.decode(), "ok")

    def test_db_mati_503(self):
        with patch("core.views.connections") as conns:
            conns.__getitem__.return_value.cursor.side_effect = Exception("db down")
            r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 503)


class CspHeaderTests(TestCase):
    """Aset 100% vendored — CSP memblokir origin eksternal total. Script/style
    inline masih dipakai template (confirm modal dll.) → 'unsafe-inline'."""

    def test_csp_header_terpasang_di_login(self):
        r = self.client.get(reverse("login"))
        csp = r.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("img-src 'self' data:", csp)
        self.assertIn("frame-ancestors 'none'", csp)
