from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase

from sources.models import Toko


class StaticAssetTests(TestCase):
    def test_fonts_css_ditemukan(self):
        self.assertIsNotNone(finders.find("web/css/fonts.css"))

    def test_font_woff2_ada(self):
        for f in [
            "web/fonts/Zodiak-Bold.woff2",
            "web/fonts/Supreme-Regular.woff2",
            "web/fonts/Supreme-Medium.woff2",
            "web/fonts/Supreme-Bold.woff2",
            "web/fonts/IBMPlexMono-Regular.woff2",
            "web/fonts/IBMPlexMono-Medium.woff2",
        ]:
            self.assertIsNotNone(finders.find(f), f)

    def test_app_css_ditemukan(self):
        self.assertIsNotNone(finders.find("web/css/app.css"))


class ShellTests(TestCase):
    def setUp(self):
        self.toko = Toko.objects.filter(is_active=True).first() or Toko.objects.create(key="lbs", name="LBS", is_active=True)
        U = get_user_model()
        self.admin = U.objects.create_superuser("uiadmin", password="rahasia-123")
        self.client.force_login(self.admin)

    def test_shell_pakai_app_css_dan_motion_js(self):
        r = self.client.get("/")
        self.assertContains(r, "web/css/app.css")
        self.assertContains(r, "web/js/motion.js")
        self.assertNotContains(r, "lenis")          # Lenis dibuang
        self.assertNotContains(r, "fonts.googleapis") # Google Fonts dibuang
        self.assertContains(r, 'class="folio"')


class DashboardUiTests(TestCase):
    def setUp(self):
        self.toko = Toko.objects.filter(is_active=True).first() or Toko.objects.create(key="lbs", name="LBS", is_active=True)
        self.admin = get_user_model().objects.create_superuser("uiadmin2", password="rahasia-123")
        self.client.force_login(self.admin)

    def test_dashboard_folio_dan_rail(self):
        r = self.client.get("/")
        self.assertContains(r, "DASBOR")
        self.assertContains(r, "hrail")
        self.assertIn("batches", r.context)

    def test_folio_upload_transaksi(self):
        self.assertContains(self.client.get("/upload/"), "UNGGAH")
        self.assertContains(self.client.get("/transactions/"), "TRANSAKSI")

    def test_folio_reconcile(self):
        r = self.client.get("/reconcile/")
        self.assertContains(r, "REKONSILIASI")
        self.assertContains(r, "recon-overlay.js")
