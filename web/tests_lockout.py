"""Brute-force login (django-axes): 5x salah password = user+IP dikunci 1 jam.

AXES_ENABLED mati saat suite tes (client.login() tanpa request tidak kompatibel
dengan backend axes) — tes di sini menyalakannya sendiri via override_settings.
"""
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

User = get_user_model()


@override_settings(AXES_ENABLED=True)
class BruteForceTests(TestCase):
    """5x salah password = akun+IP dikunci sementara (429)."""

    def setUp(self):
        User.objects.create_user("adm", password="pw123456", role="admin")

    def test_lockout_setelah_5_gagal(self):
        url = reverse("login")
        for _ in range(5):
            self.client.post(url, {"username": "adm", "password": "salah"})
        # Percobaan ke-6 dengan password BENAR pun ditolak — lockout aktif.
        r = self.client.post(url, {"username": "adm", "password": "pw123456"})
        self.assertEqual(r.status_code, 429)

    def test_halaman_lockout_bahasa_indonesia(self):
        """W6-8a: halaman lockout pakai template Indonesia sendiri, bukan
        respons default axes berbahasa Inggris."""
        url = reverse("login")
        for _ in range(5):
            self.client.post(url, {"username": "adm", "password": "salah"})
        r = self.client.post(url, {"username": "adm", "password": "pw123456"})
        self.assertEqual(r.status_code, 429)
        self.assertContains(r, "Terlalu banyak percobaan login", status_code=429)
        self.assertContains(r, "1 jam", status_code=429)

    def test_di_bawah_limit_tetap_bisa_login(self):
        url = reverse("login")
        for _ in range(3):
            self.client.post(url, {"username": "adm", "password": "salah"})
        r = self.client.post(url, {"username": "adm", "password": "pw123456"})
        self.assertEqual(r.status_code, 302)  # sukses → redirect


class ClientIpTests(TestCase):
    """Di belakang proxy Railway REMOTE_ADDR = IP internal load balancer yang
    berganti-ganti — lockout kombo username+IP tak pernah akumulasi. IP klien
    diambil dari hop TERAKHIR X-Forwarded-For (ditulis edge Railway; entri
    kiriman penyerang berada di depannya dan tidak dipercaya)."""

    def _req(self, **meta):
        r = RequestFactory().get("/")
        r.META.update(meta)
        return r

    def test_xff_spoof_diabaikan_ambil_hop_terakhir(self):
        from truth_auditor.security import client_ip

        r = self._req(HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9",
                      REMOTE_ADDR="100.64.0.3")
        self.assertEqual(client_ip(r), "203.0.113.9")

    def test_tanpa_xff_pakai_remote_addr(self):
        from truth_auditor.security import client_ip

        r = self._req(REMOTE_ADDR="127.0.0.1")
        self.assertEqual(client_ip(r), "127.0.0.1")
