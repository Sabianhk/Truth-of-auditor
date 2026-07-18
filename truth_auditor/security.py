"""Helper keamanan yang harus fail-safe — dipisah dari settings.py supaya
bisa diuji unit tanpa reload modul settings."""


def client_ip(request):
    """IP klien di belakang proxy Railway (utk lockout django-axes).

    REMOTE_ADDR = IP internal load balancer (berganti-ganti). Ambil hop
    TERAKHIR X-Forwarded-For: itu yang ditulis edge Railway (proxy tepercaya
    tunggal); entri kiriman penyerang berada di DEPAN dan diabaikan — spoof
    XFF tidak bisa dipakai menghindari lockout.
    """
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")
