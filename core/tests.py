from unittest.mock import patch

from django.test import TestCase


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
