import re

from django.views.decorators.http import require_http_methods

from main.auth import verify_app_jwt
from main.constants import DATA_FIELD_KINDS
from main.errors import ApiError, ValidationApiError
from main.views import (
    api_endpoint,
    assert_field_auth,
    assert_profile_auth,
    assert_wallet_auth,
    authenticated,
    get_bearer_token,
    json_ok,
    optional_address,
    parse_json,
    require_keys,
    validate_address,
)
from data import repository
from data.integrations import confirm_verification, start_verification
from data.orders import create_order, get_export_plan, summarize_order
from data.search import build_quote, build_quote_detail, extend_quote

UINT_RE = re.compile(r"^\d+$")
DATA_FIELD_KIND_SET = set(DATA_FIELD_KINDS)


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
    if body["kind"] not in DATA_FIELD_KIND_SET:
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
@require_http_methods(["POST"])
def search_quote(request):
    body = parse_json(request)
    require_keys(body, ["prompt"])
    if body.get("wantedFields") and not set(body["wantedFields"]).issubset(DATA_FIELD_KIND_SET):
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
@require_http_methods(["POST"])
def search_request_extend(request, quote_id):
    body = parse_json(request)
    require_keys(body, ["prompt"])
    quote = repository.get_quote(quote_id)
    if not quote:
        raise ApiError("request_not_found", status_code=404)
    buyer_wallet = validate_address(quote.get("buyerWallet"), "buyerWallet")
    assert_wallet_auth(request.app_auth, buyer_wallet)
    return json_ok({"request": extend_quote(quote, body["prompt"].strip())})


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
    log = repository.save_export_log(order["id"], body)
    repository.update_order({**order, "status": "exported"})
    return json_ok({"orderId": order["id"], "status": "exported", "log": log}, status=201)


@api_endpoint
@require_http_methods(["GET"])
def public_card(_request, slug):
    card = repository.get_public_card(slug)
    if not card:
        return json_ok({"error": "public_card_not_found"}, status=404)
    return json_ok({"profile": card})
