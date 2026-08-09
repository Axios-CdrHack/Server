import json
import re

from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods
import requests

from data import repository
from data.search import GeminiIntentError

from .auth import verify_app_jwt
from .errors import ApiError, InvalidAuthTokenError, LicenseVerificationError, ProviderNotConfiguredError, ValidationApiError

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
TX_HASH_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
CDR_API_PATH_RE = re.compile(r"^/dkg/(?:latest_active|dkg_network|global_public_key|registrations(?:/verified)?|cdr_partials)$")
CDR_API_BASE_URL = "http://172.192.41.96:1317"


def json_ok(payload, status=200):
    return JsonResponse(repository.strip_none(payload), status=status)


def parse_json(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except Exception as exc:
        raise ValidationApiError(issues=[{"message": "Invalid JSON"}]) from exc


def require_keys(body, keys):
    missing = [key for key in keys if key not in body or body[key] in (None, "")]
    if missing:
        raise ValidationApiError(issues=[{"path": [key], "message": "Required"} for key in missing])


def validate_address(value, key="address"):
    if not isinstance(value, str) or not ADDRESS_RE.fullmatch(value):
        raise ValidationApiError(issues=[{"path": [key], "message": "Invalid address"}])
    return value


def optional_address(value, key):
    if value in (None, ""):
        return None
    return validate_address(value, key)


def validate_tx_hash(value, key="txHash"):
    if not isinstance(value, str) or not TX_HASH_RE.fullmatch(value):
        raise ValidationApiError(issues=[{"path": [key], "message": "Invalid transaction hash"}])
    return value


def validate_public_fields(fields):
    if not isinstance(fields, dict):
        raise ValidationApiError(issues=[{"path": ["publicFields"], "message": "Required"}])
    issues = []
    if not str(fields.get("name", "")).strip():
        issues.append({"path": ["publicFields", "name"], "message": "Name is required"})
    if not str(fields.get("gender", "")).strip():
        issues.append({"path": ["publicFields", "gender"], "message": "Gender is required"})
    try:
        age = int(fields.get("age"))
    except Exception:
        age = 0
    if age < 1 or age > 120:
        issues.append({"path": ["publicFields", "age"], "message": "Age must be between 1 and 120"})
    if issues:
        raise ValidationApiError(issues=issues)
    fields["age"] = age
    return fields


def get_bearer_token(request):
    auth = request.headers.get("Authorization", "")
    return auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else None


def wallet_matches_auth(auth, wallet):
    if not wallet:
        return False
    normalized = wallet.lower()
    return normalized in [
        (auth.get("walletAddress") or "").lower(),
        (auth.get("smartWalletAddress") or "").lower(),
    ]


def profile_matches_auth(auth, profile):
    if not profile:
        return False
    return any(wallet_matches_auth(auth, profile.get(key)) for key in ["walletAddress", "smartWalletAddress", "payoutAddress"])


def profile_match_score(auth, profile):
    if not profile:
        return 0
    if wallet_matches_auth(auth, profile.get("smartWalletAddress")):
        return 2
    if wallet_matches_auth(auth, profile.get("walletAddress")) or wallet_matches_auth(auth, profile.get("payoutAddress")):
        return 1
    return 0


def field_matches_auth(auth, field):
    return bool(field and profile_matches_auth(auth, repository.get_profile(field.get("userId"))))


def assert_wallet_auth(auth, wallet):
    if not wallet_matches_auth(auth, wallet):
        raise ApiError("wallet_not_authorized", status_code=403)


def assert_profile_auth(auth, profile_id):
    if not profile_id:
        raise ApiError("profile_id_required", status_code=400)
    if not profile_matches_auth(auth, repository.get_profile(profile_id)):
        raise ApiError("profile_not_authorized", status_code=403)


def assert_field_auth(auth, field_id):
    fields = repository.get_fields_by_ids([field_id])
    if not fields or not field_matches_auth(auth, fields[0]):
        raise ApiError("field_not_authorized", status_code=403)


def sse_message(event, payload):
    data = json.dumps(repository.strip_none(payload), ensure_ascii=False)
    return f"event: {event}\ndata: {data}\n\n"


def api_error_payload(error):
    if isinstance(error, ValidationApiError):
        return {"error": "validation_error", "issues": error.issues or []}
    if isinstance(error, ProviderNotConfiguredError):
        return {"error": "provider_not_configured", "message": error.message or str(error)}
    if isinstance(error, InvalidAuthTokenError):
        return {"error": "invalid_auth_token"}
    if isinstance(error, LicenseVerificationError):
        return {"error": "license_verification_failed", "message": error.message or str(error)}
    if isinstance(error, GeminiIntentError):
        return {"error": "upstream_error", "message": str(error)}
    if isinstance(error, ApiError):
        payload = {"error": error.code}
        if error.message:
            payload["message"] = error.message
        if error.issues:
            payload["issues"] = error.issues
        return payload
    return {"error": "server_error", "message": str(error)}


def api_endpoint(fn):
    def wrapper(request, *args, **kwargs):
        try:
            return fn(request, *args, **kwargs)
        except ValidationApiError as error:
            return json_ok({"error": "validation_error", "issues": error.issues or []}, status=400)
        except ProviderNotConfiguredError as error:
            return json_ok({"error": "provider_not_configured", "message": error.message or str(error)}, status=503)
        except InvalidAuthTokenError:
            return json_ok({"error": "invalid_auth_token"}, status=401)
        except LicenseVerificationError as error:
            return json_ok({"error": "license_verification_failed", "message": error.message or str(error)}, status=402)
        except GeminiIntentError as error:
            return json_ok({"error": "upstream_error", "message": str(error)}, status=502)
        except ApiError as error:
            payload = {"error": error.code}
            if error.message:
                payload["message"] = error.message
            if error.issues:
                payload["issues"] = error.issues
            return json_ok(payload, status=error.status_code)
        except Exception:
            raise

    return wrapper


def authenticated(fn):
    def wrapper(request, *args, **kwargs):
        token = get_bearer_token(request)
        if not token:
            return json_ok({"error": "app_auth_required"}, status=401)
        request.app_auth = verify_app_jwt(token)
        return fn(request, *args, **kwargs)

    return wrapper


@api_endpoint
@require_http_methods(["GET"])
def health(_request):
    return json_ok({"ok": True, "service": "axios-data-card-api"})


@api_endpoint
@require_http_methods(["GET"])
def cdr_api_proxy(request, path):
    upstream_path = "/" + path
    if not CDR_API_PATH_RE.fullmatch(upstream_path):
        return json_ok({"error": "cdr_api_path_not_allowed"}, status=404)
    query = request.META.get("QUERY_STRING", "")
    url = f"{CDR_API_BASE_URL}{'/' + path}{'?' + query if query else ''}"
    upstream = requests.get(url, headers={"Accept": "application/json"}, timeout=30)
    response = HttpResponse(upstream.content, status=upstream.status_code, content_type=upstream.headers.get("content-type", "application/json"))
    response["Cache-Control"] = "no-store"
    return response
