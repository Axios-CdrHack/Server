from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from api.auth import exchange_privy_access_token
from api.errors import ApiError, ValidationApiError
from api.views import (
    api_endpoint,
    assert_wallet_auth,
    authenticated,
    json_ok,
    parse_json,
    profile_match_score,
    profile_matches_auth,
    validate_address,
    validate_public_fields,
    wallet_matches_auth,
)
from users import repository
from users.integrations import create_wallet_link_proof, upload_profile_image


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
