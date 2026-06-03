from urllib.parse import urlparse
import os

from django.http import JsonResponse


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def normalize_origin(value):
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def allowed_origins():
    raw = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:3001,http://localhost:3002,http://localhost:3003")
    return {origin.strip() for origin in raw.split(",") if origin.strip()}


def request_origin(request):
    origin = request.headers.get("Origin")
    if origin:
        return normalize_origin(origin)
    referer = request.headers.get("Referer")
    return normalize_origin(referer) if referer else None


class CorsAndOriginMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request_origin(request)
        allowed = allowed_origins()
        if request.method == "OPTIONS":
            response = JsonResponse({}, status=204)
        elif request.method not in SAFE_METHODS and (not origin or origin not in allowed):
            response = JsonResponse({"error": "csrf_origin_not_allowed"}, status=403)
        else:
            response = self.get_response(request)

        if origin and origin in allowed:
            response["Access-Control-Allow-Origin"] = origin
            response["Vary"] = "Origin"
            response["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response
