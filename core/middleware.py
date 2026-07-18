"""Middleware keamanan lintas-app."""

# Semua aset di-vendor (tanpa CDN/origin eksternal) — CSP bisa ketat.
# 'unsafe-inline' untuk script/style: template masih memakai blok <script>/
# <style> inline (confirm modal, guard form); origin eksternal tetap
# terblokir total. Jangan tambah CDN — vendorkan aset baru.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", _CSP)
        return response
