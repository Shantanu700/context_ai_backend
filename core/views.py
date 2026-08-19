from django.db import connection
from django.http import JsonResponse


def health(request):
    try:
        connection.ensure_connection()
    except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
        return JsonResponse({"status": "error", "db": str(exc)}, status=503)
    return JsonResponse({"status": "ok", "db": "ok"})
