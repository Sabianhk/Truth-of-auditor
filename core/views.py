from django.db import connections
from django.http import HttpResponse


def healthz(request):
    """Healthcheck deploy: proses hidup + DB terjangkau. Tanpa auth — dipakai
    Railway untuk memutuskan rollout; gagal = deploy baru tidak menerima traffic."""
    try:
        with connections["default"].cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:  # noqa: BLE001 — DB apa pun error = tidak sehat, detail di log
        return HttpResponse("db-error", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")
