import json
import re

from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_http_methods
import requests

from . import repository
from .auth import exchange_privy_access_token, verify_app_jwt
from .constants import PURCHASE_CONTRACT_ADDRESS, STORY_AENEID_RPC_URL
from .errors import ApiError, InvalidAuthTokenError, LicenseVerificationError, ProviderNotConfiguredError, ValidationApiError
from .integrations import (
    confirm_verification,
    create_wallet_link_proof,
    deploy_field_cdr_with_server_wallet,
    list_onchain_sales_by_wallet,
    start_verification,
    upload_field_ip_metadata,
    upload_profile_image,
)
from .orders import create_order, get_export_plan, summarize_order
from .search import GeminiIntentError, build_quote, build_quote_detail

ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
TX_HASH_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")
UINT_RE = re.compile(r"^\d+$")
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
    auth_privy_id = auth.get("privyUserId") or auth.get("sub")
    if auth_privy_id and profile.get("privyUserId") == auth_privy_id:
        return True
    if auth.get("email") and profile.get("email", "").lower() == auth["email"]:
        return True
    return any(wallet_matches_auth(auth, profile.get(key)) for key in ["walletAddress", "smartWalletAddress", "payoutAddress"])


def profile_match_score(auth, profile):
    if not profile:
        return 0
    auth_privy_id = auth.get("privyUserId") or auth.get("sub")
    if auth_privy_id and profile.get("privyUserId") == auth_privy_id:
        return 4
    if auth.get("email") and profile.get("email", "").lower() == auth["email"]:
        return 3
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
@require_http_methods(["POST"])
def privy_exchange(request):
    body = parse_json(request)
    token = body.get("privyAccessToken")
    if not isinstance(token, str) or len(token) < 20:
        raise ValidationApiError(issues=[{"path": ["privyAccessToken"], "message": "Required"}])
    session = exchange_privy_access_token(token)
    user = repository.upsert_user_by_email(session["email"]) if session.get("email") else None
    if session.get("email") and session.get("walletAddress"):
        connected = repository.connect_wallet_to_user(session["email"], session["walletAddress"])
        if connected == "wallet_conflict":
            return json_ok({"error": "wallet_already_connected", "message": "This wallet is already linked to another email."}, status=409)
        if connected == "wallet_locked":
            return json_ok({"error": "wallet_link_locked", "message": "This email account already has an immutable linked wallet."}, status=409)
        user = connected
    return json_ok({"session": session, "user": user}, status=201)


@api_endpoint
@require_http_methods(["GET"])
def cdr_api_proxy(request, path):
    upstream_path = "/" + path
    if not CDR_API_PATH_RE.fullmatch(upstream_path):
        return json_ok({"error": "cdr_api_path_not_allowed"}, status=404)
    query = request.META.get("QUERY_STRING", "")
    url = f"{CDR_API_BASE_URL}{upstream_path}{'?' + query if query else ''}"
    upstream = requests.get(url, headers={"Accept": "application/json"}, timeout=30)
    response = HttpResponse(upstream.content, status=upstream.status_code, content_type=upstream.headers.get("content-type", "application/json"))
    response["Cache-Control"] = "no-store"
    return response


@api_endpoint
@require_http_methods(["GET"])
def profiles(_request):
    public_profiles = [
        {
            "id": profile["id"],
            "publicSlug": profile["publicSlug"],
            "displayName": profile["displayName"],
            "avatarUrl": profile.get("avatarUrl"),
            "publicFields": profile["publicFields"],
        }
        for profile in repository.list_profiles()
    ]
    return json_ok({"profiles": public_profiles})


@api_endpoint
@authenticated
@require_http_methods(["GET"])
def my_profile(request):
    matches = [(profile_match_score(request.app_auth, profile), profile) for profile in repository.list_profiles()]
    matches = [(score, profile) for score, profile in matches if score > 0]
    if not matches:
        return JsonResponse({"profile": None, "fields": []})
    profile = sorted(matches, key=lambda item: item[0], reverse=True)[0][1]
    return json_ok({"profile": profile, "fields": repository.get_fields_by_profile_id(profile["id"])})


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def email_session(request):
    body = parse_json(request)
    email = str(body.get("email", "")).strip().lower()
    if not email or "@" not in email:
        raise ValidationApiError(issues=[{"path": ["email"], "message": "Invalid email"}])
    if not request.app_auth.get("email") or email != request.app_auth["email"]:
        raise ApiError("email_not_authorized", status_code=403)
    return json_ok({"user": repository.upsert_user_by_email(email)}, status=201)


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def wallet_user(request):
    body = parse_json(request)
    email = str(body.get("email", "")).strip().lower()
    wallet = validate_address(body.get("walletAddress"), "walletAddress")
    if not request.app_auth.get("email") or email != request.app_auth["email"]:
        raise ApiError("email_not_authorized", status_code=403)
    if not wallet_matches_auth(request.app_auth, wallet):
        raise ApiError("wallet_not_authorized", status_code=403)
    repository.upsert_user_by_email(email)
    user = repository.connect_wallet_to_user(email, wallet)
    if user == "wallet_conflict":
        return json_ok({"error": "wallet_already_connected", "message": "This wallet is already linked to another email."}, status=409)
    if user == "wallet_locked":
        return json_ok({"error": "wallet_link_locked", "message": "This email account already has an immutable linked wallet."}, status=409)
    proof = create_wallet_link_proof(user["email"], user["id"], user.get("walletAddress") or wallet)
    return json_ok({"user": user, "walletLinkProof": proof}, status=201)


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def upsert_profile(request):
    body = parse_json(request)
    fields = validate_public_fields(body.get("publicFields"))
    body["publicFields"] = fields
    if body.get("id") and not profile_matches_auth(request.app_auth, repository.get_profile(body["id"])):
        raise ApiError("profile_not_authorized", status_code=403)
    for key in ["walletAddress", "smartWalletAddress"]:
        if body.get(key) and not wallet_matches_auth(request.app_auth, body[key]):
            raise ApiError("wallet_not_authorized", status_code=403)
    profile = repository.upsert_profile({
        **body,
        "email": request.app_auth.get("email") or body.get("email"),
        "privyUserId": request.app_auth.get("privyUserId") or request.app_auth.get("sub"),
    })
    return json_ok({"profile": profile}, status=201)


def profiles_endpoint(request, *args, **kwargs):
    """Dispatch /profiles: public GET list, authenticated POST upsert (mirrors the Express route)."""
    if request.method == "POST":
        return upsert_profile(request, *args, **kwargs)
    return profiles(request, *args, **kwargs)


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def upload_avatar(request):
    body = parse_json(request)
    owner = validate_address(body.get("ownerWallet"), "ownerWallet")
    assert_wallet_auth(request.app_auth, owner)
    uploaded = upload_profile_image(owner, body.get("fileName") or "", body.get("mimeType"), body.get("dataBase64") or "")
    return json_ok(uploaded, status=201)


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def field_ip_metadata(request):
    body = parse_json(request)
    require_keys(body, ["profileId", "kind"])
    if body["kind"] not in {"email", "mobile", "telegram", "discord", "twitter"}:
        raise ValidationApiError(issues=[{"path": ["kind"], "message": "Invalid field kind"}])
    assert_profile_auth(request.app_auth, body["profileId"])
    metadata = upload_field_ip_metadata(body["profileId"], body["kind"], str(body.get("label") or body["kind"]))
    return json_ok({"metadata": metadata}, status=201)


@api_endpoint
@authenticated
@require_http_methods(["GET"])
def profile_fields(request, profile_id):
    assert_profile_auth(request.app_auth, profile_id)
    profile = repository.get_profile(profile_id)
    if not profile:
        return json_ok({"error": "profile_not_found"}, status=404)
    return json_ok({"profile": profile, "fields": repository.get_fields_by_profile_id(profile_id)})


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def fields(request):
    body = parse_json(request)
    require_keys(body, ["kind", "label", "accessMode", "priceCents"])
    if body["kind"] not in {"email", "mobile", "telegram", "discord", "twitter"}:
        raise ValidationApiError(issues=[{"path": ["kind"], "message": "Invalid field kind"}])
    profile_id = body.get("profileId") or body.get("userId")
    assert_profile_auth(request.app_auth, profile_id)
    for key in ["cdrLicenseIpId", "sellerAddress", "platformWallet", "ipaRecipient", "ipaNftContract"]:
        body[key] = optional_address(body.get(key), key)
    if body.get("cdrLicenseTermsId") in ("", None):
        body["cdrLicenseTermsId"] = None
    elif not UINT_RE.fullmatch(str(body["cdrLicenseTermsId"])):
        raise ValidationApiError(issues=[{"path": ["cdrLicenseTermsId"], "message": "Invalid uint"}])
    return json_ok({"field": repository.upsert_field(body)}, status=201)


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def verify_start(request):
    body = parse_json(request)
    require_keys(body, ["profileId", "channel", "target"])
    assert_profile_auth(request.app_auth, body["profileId"])
    if body["channel"] not in {"email", "mobile"}:
        raise ValidationApiError(issues=[{"path": ["channel"], "message": "Invalid channel"}])
    return json_ok(start_verification(body["channel"], body["target"]), status=201)


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def verify_confirm(request):
    body = parse_json(request)
    require_keys(body, ["verificationId", "fieldId", "code"])
    assert_field_auth(request.app_auth, body["fieldId"])
    if not confirm_verification(body["verificationId"], body["code"]):
        return json_ok({"error": "invalid_verification_code"}, status=400)
    field = repository.mark_field_verified(body["fieldId"])
    if not field:
        return json_ok({"error": "field_not_found"}, status=404)
    return json_ok({"field": field})


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def cdr_deploy_log(request):
    body = parse_json(request)
    require_keys(body, ["fieldId", "cdrVaultUuid", "deployTxHash"])
    if not UINT_RE.fullmatch(str(body["cdrVaultUuid"])):
        raise ValidationApiError(issues=[{"path": ["cdrVaultUuid"], "message": "Invalid CDR uuid"}])
    validate_tx_hash(body["deployTxHash"], "deployTxHash")
    assert_field_auth(request.app_auth, body["fieldId"])
    field = repository.save_deploy_log(body["fieldId"], body["cdrVaultUuid"], body["deployTxHash"])
    if not field:
        return json_ok({"error": "field_not_deployable"}, status=400)
    return json_ok({"field": field}, status=201)


def build_cdr_server_deploy_payload(app_auth, field_id):
    assert_field_auth(app_auth, field_id)
    fields = repository.get_fields_by_ids([field_id])
    field = fields[0] if fields else None
    if not field:
        raise ApiError("field_not_found", status_code=404)
    if not repository.field_can_start_cdr_deploy(field):
        raise ApiError("field_not_deployable", status_code=400)
    has_existing_deployment = bool(
        field.get("cdrVaultUuid")
        and field.get("deployTxHash")
        and field.get("cdrLicenseIpId")
        and field.get("cdrLicenseTermsId")
    )
    if has_existing_deployment:
        raise ApiError("field_already_issued", status_code=409)

    profile = repository.get_profile(field["userId"])
    recipient = (profile.get("walletAddress") if profile else None) or app_auth.get("walletAddress")
    recipient = validate_address(recipient, "recipient")
    if not wallet_matches_auth(app_auth, recipient):
        raise ApiError("wallet_not_authorized", status_code=403)

    return field, {
        "fieldId": field["id"],
        "profileId": field["userId"],
        "kind": field["kind"],
        "label": field["label"],
        "value": field["valuePreview"],
        "priceCents": field["priceCents"],
        "recipient": recipient,
    }


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def cdr_server_deploy(request):
    body = parse_json(request)
    require_keys(body, ["fieldId"])
    field, payload = build_cdr_server_deploy_payload(request.app_auth, body["fieldId"])
    metadata = upload_field_ip_metadata(field["userId"], field["kind"], field["label"])
    payload["ipMetadata"] = metadata["ipMetadata"]
    deployment = deploy_field_cdr_with_server_wallet(payload)
    saved = repository.save_server_cdr_deployment(field["id"], deployment)
    if not saved:
        return json_ok({"error": "field_not_deployable"}, status=400)
    return json_ok({"field": saved, "deployment": deployment}, status=201)


@api_endpoint
@authenticated
@require_http_methods(["GET"])
def cdr_server_deploy_events(request):
    field_id = request.GET.get("fieldId")
    if not field_id:
        raise ValidationApiError(issues=[{"path": ["fieldId"], "message": "Required"}])
    field, payload = build_cdr_server_deploy_payload(request.app_auth, field_id)

    def stream():
        try:
            yield sse_message("status", {"status": "metadata", "message": "Preparing metadata"})
            metadata = upload_field_ip_metadata(field["userId"], field["kind"], field["label"])
            payload["ipMetadata"] = metadata["ipMetadata"]

            yield sse_message("status", {"status": "deploying", "message": "Server minting IPA + CDR"})
            deployment = deploy_field_cdr_with_server_wallet(payload)

            yield sse_message("status", {"status": "saving", "message": "Saving deployment"})
            saved = repository.save_server_cdr_deployment(field["id"], deployment)
            if not saved:
                yield sse_message("error", {"error": "field_not_deployable"})
                return

            yield sse_message("complete", {"status": "complete", "field": saved, "deployment": deployment})
        except Exception as error:
            yield sse_message("error", api_error_payload(error))

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def cdr_toggle(request):
    body = parse_json(request)
    require_keys(body, ["fieldId", "cdrState"])
    if body["cdrState"] not in {"off", "deploying", "on"}:
        raise ValidationApiError(issues=[{"path": ["cdrState"], "message": "Invalid CDR state"}])
    assert_field_auth(request.app_auth, body["fieldId"])
    field = repository.set_cdr_state(body["fieldId"], body["cdrState"])
    if not field:
        return json_ok({"error": "field_not_toggleable"}, status=400)
    return json_ok({"field": field})


@api_endpoint
@require_http_methods(["POST"])
def search_quote(request):
    body = parse_json(request)
    require_keys(body, ["prompt"])
    if body.get("wantedFields") and not set(body["wantedFields"]).issubset({"email", "mobile", "telegram", "discord", "twitter"}):
        raise ValidationApiError(issues=[{"path": ["wantedFields"], "message": "Invalid field kind"}])
    if body.get("buyerWallet"):
        buyer_wallet = validate_address(body["buyerWallet"], "buyerWallet")
        token = get_bearer_token(request)
        if not token:
            return json_ok({"error": "app_auth_required"}, status=401)
        assert_wallet_auth(verify_app_jwt(token), buyer_wallet)
    return json_ok(build_quote(body))


@api_endpoint
@authenticated
@require_http_methods(["GET"])
def search_requests(request):
    wallet = validate_address(request.GET.get("wallet"), "wallet")
    assert_wallet_auth(request.app_auth, wallet)
    return json_ok({"requests": repository.list_quotes_by_buyer_wallet(wallet)})


@api_endpoint
@authenticated
@require_http_methods(["GET"])
def search_request_detail(request, quote_id):
    quote = repository.get_quote(quote_id)
    if not quote:
        raise ApiError("request_not_found", status_code=404)
    buyer_wallet = validate_address(quote.get("buyerWallet"), "buyerWallet")
    assert_wallet_auth(request.app_auth, buyer_wallet)
    return json_ok({"request": build_quote_detail(quote)})


@api_endpoint
@authenticated
@require_http_methods(["GET", "POST"])
def orders(request):
    if request.method == "POST":
        return create_order_view(request)
    buyer_wallet = validate_address(request.GET.get("buyerWallet"), "buyerWallet")
    assert_wallet_auth(request.app_auth, buyer_wallet)
    return json_ok({"orders": [summarize_order(order) for order in repository.list_orders_by_buyer_wallet(buyer_wallet)]})


def create_order_view(request):
    body = parse_json(request)
    require_keys(body, ["buyerWallet", "prompt", "wantedFields"])
    buyer_wallet = validate_address(body["buyerWallet"], "buyerWallet")
    assert_wallet_auth(request.app_auth, buyer_wallet)
    order = create_order(
        body,
        owner_wallets=[wallet for wallet in [buyer_wallet, request.app_auth.get("walletAddress"), request.app_auth.get("smartWalletAddress")] if wallet],
    )
    return json_ok(
        {
            "order": order,
            "payment": {
                "contract": order["purchaseContract"],
                "buyerPaysGas": True,
                "platformFeeBps": order["platformFeeBps"],
                "sellerPayouts": order["sellerPayouts"],
            },
        },
        status=201,
    )


@api_endpoint
@authenticated
@require_http_methods(["GET"])
def export_plan(request, order_id):
    order = repository.get_order(order_id)
    if not order:
        return json_ok({"error": "order_not_found"}, status=404)
    assert_wallet_auth(request.app_auth, order["buyerWallet"])
    plan = get_export_plan(order_id)
    return json_ok(plan if plan else {"error": "order_not_found"}, status=200 if plan else 404)


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def export_log(request, order_id):
    order = repository.get_order(order_id)
    if not order:
        return json_ok({"error": "order_not_found"}, status=404)
    assert_wallet_auth(request.app_auth, order["buyerWallet"])
    body = parse_json(request)
    require_keys(body, ["generatedAt", "successfulFieldIds", "failedFieldIds", "format"])
    repository.update_order({**order, "status": "exported"})
    return json_ok({"orderId": order["id"], "status": "exported", "log": body}, status=201)


def merge_sales(server_sales, onchain_sales):
    sales = {}
    for sale in server_sales + onchain_sales:
        key = f"{sale.get('paymentTxHash')}:{sale.get('fieldId')}" if sale.get("paymentTxHash") else sale["id"]
        sales[key] = sale
    return sorted(sales.values(), key=lambda item: item.get("createdAt", ""), reverse=True)


@api_endpoint
@authenticated
@require_http_methods(["GET"])
def sales(request):
    wallet = validate_address(request.GET.get("wallet"), "wallet")
    assert_wallet_auth(request.app_auth, wallet)
    server_sales = repository.list_sales_by_wallet(wallet)
    onchain_sales = list_onchain_sales_by_wallet(wallet)
    return json_ok(
        {
            "sales": merge_sales(server_sales, onchain_sales),
            "onchain": {"rpcUrl": STORY_AENEID_RPC_URL, "contract": PURCHASE_CONTRACT_ADDRESS, "logCount": len(onchain_sales)},
        }
    )


@api_endpoint
@require_http_methods(["GET"])
def public_card(_request, slug):
    card = repository.get_public_card(slug)
    if not card:
        return json_ok({"error": "public_card_not_found"}, status=404)
    return json_ok({"profile": card})
