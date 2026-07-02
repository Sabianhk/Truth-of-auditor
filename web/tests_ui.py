from django.contrib.staticfiles import finders
from django.test import TestCase


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
