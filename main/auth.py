from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import os
import re
import time

import jwt
import requests

from .errors import InvalidAuthTokenError, ProviderNotConfiguredError

APP_JWT_ISSUER = "axios-data-card-api"
APP_JWT_AUDIENCE = "axios-data-card-client"
APP_JWT_TTL_SECONDS = 60 * 60
PRIVY_AUTH_API_BASE_URL = "https://auth.privy.io"
PRIVY_CLIENT_HEADER = "django:local"
_PRIVY_VERIFICATION_KEY_CACHE = {}


def read_required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ProviderNotConfiguredError(message=f"{name.lower()}_not_configured")
    return value


def privy_api_base_url():
    return os.environ.get("PRIVY_API_BASE_URL", "https://api.privy.io").rstrip("/")


def normalize_pem_public_key(value):
    if not isinstance(value, str):
        raise ProviderNotConfiguredError(message="privy_verification_key_missing")

    key = value.strip()
    begin = "-----BEGIN PUBLIC KEY-----"
    end = "-----END PUBLIC KEY-----"
    if not key.startswith(begin) or not key.endswith(end):
        raise ProviderNotConfiguredError(message="privy_verification_key_invalid")

    body = key[len(begin) : -len(end)]
    body = "".join(body.split())
    if not body:
        raise ProviderNotConfiguredError(message="privy_verification_key_invalid")
    lines = [body[index : index + 64] for index in range(0, len(body), 64)]
    return f"{begin}\n" + "\n".join(lines) + f"\n{end}\n"


def get_privy_verification_key(app_id):
    cached = _PRIVY_VERIFICATION_KEY_CACHE.get(app_id)
    if cached:
        return cached

    try:
        response = requests.get(
            f"{PRIVY_AUTH_API_BASE_URL}/api/v1/apps/{app_id}",
            headers={"Accept": "application/json", "privy-app-id": app_id, "privy-client": PRIVY_CLIENT_HEADER},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ProviderNotConfiguredError(message="privy_app_config_unavailable") from exc
    if response.status_code >= 400:
        raise ProviderNotConfiguredError(message="privy_app_config_unavailable")

    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderNotConfiguredError(message="privy_app_config_invalid") from exc
    verification_key = normalize_pem_public_key(body.get("verification_key"))
    _PRIVY_VERIFICATION_KEY_CACHE[app_id] = verification_key
    return verification_key


def base64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def normalize_email(value):
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    return email if "@" in email else None


def normalize_address(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if re.fullmatch(r"0x[a-fA-F0-9]{40}", value) else None


def account_email(account):
    if not isinstance(account, dict):
        return None
    if account.get("type") == "email":
        return normalize_email(account.get("address"))
    return normalize_email(account.get("email"))


def account_address(account):
    if not isinstance(account, dict):
        return None
    return normalize_address(account.get("address"))


def pick_email(user):
    for account in user.get("linked_accounts", []) or []:
        email = account_email(account)
        if email:
            return email
    return None


def pick_wallet_address(user):
    accounts = user.get("linked_accounts", []) or []
    embedded = next(
        (
            account
            for account in accounts
            if isinstance(account, dict)
            and account.get("type") == "wallet"
            and account.get("chain_type") == "ethereum"
            and account.get("wallet_client") == "privy"
        ),
        None,
    )
    any_eth = next(
        (
            account
            for account in accounts
            if isinstance(account, dict) and account.get("type") == "wallet" and account.get("chain_type") == "ethereum"
        ),
        None,
    )
    return account_address(embedded) or account_address(any_eth)


def pick_smart_wallet_address(user):
    accounts = user.get("linked_accounts", []) or []
    smart_wallet = next((account for account in accounts if isinstance(account, dict) and account.get("type") == "smart_wallet"), None)
    return account_address(smart_wallet)


def sign_app_jwt(input_data):
    now = int(time.time())
    exp = min(int(input_data["expiresAtSeconds"]), now + APP_JWT_TTL_SECONDS)
    payload = {
        "sub": input_data["privyUserId"],
        "sid": input_data["sessionId"],
        "email": input_data.get("email"),
        "walletAddress": input_data.get("walletAddress"),
        "smartWalletAddress": input_data.get("smartWalletAddress"),
        "iss": APP_JWT_ISSUER,
        "aud": APP_JWT_AUDIENCE,
        "iat": now,
        "exp": exp,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    header = {"alg": "HS256", "typ": "JWT"}
    unsigned = f"{base64url(json.dumps(header, separators=(',', ':')).encode())}.{base64url(json.dumps(payload, separators=(',', ':')).encode())}"
    signature = hmac.new(read_required_env("PRIVY_APP_SECRET").encode(), unsigned.encode(), hashlib.sha256).digest()
    token = f"{unsigned}.{base64url(signature)}"
    return {
        "token": token,
        "expiresAt": datetime.fromtimestamp(exp, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "payload": payload,
    }


def verify_app_jwt(token):
    try:
        payload = jwt.decode(
            token,
            read_required_env("PRIVY_APP_SECRET"),
            algorithms=["HS256"],
            issuer=APP_JWT_ISSUER,
            audience=APP_JWT_AUDIENCE,
        )
    except Exception as exc:
        raise InvalidAuthTokenError() from exc

    if not isinstance(payload.get("sub"), str) or not isinstance(payload.get("sid"), str):
        raise InvalidAuthTokenError()
    return {
        "sub": payload["sub"],
        "privyUserId": payload["sub"],
        "sid": payload["sid"],
        "email": normalize_email(payload.get("email")),
        "walletAddress": normalize_address(payload.get("walletAddress")),
        "smartWalletAddress": normalize_address(payload.get("smartWalletAddress")),
        "iss": APP_JWT_ISSUER,
        "aud": APP_JWT_AUDIENCE,
        "iat": payload.get("iat"),
        "exp": payload.get("exp"),
    }


def verify_privy_access_token(access_token):
    app_id = read_required_env("PRIVY_APP_ID")
    try:
        verification_key = get_privy_verification_key(app_id)
        payload = jwt.decode(
            access_token,
            verification_key,
            algorithms=["ES256"],
            issuer="privy.io",
            audience=app_id,
            options={"require": ["sub", "sid", "iat", "exp"]},
        )
    except ProviderNotConfiguredError:
        raise
    except Exception as exc:
        raise InvalidAuthTokenError() from exc
    return {
        "app_id": payload.get("aud"),
        "issuer": payload.get("iss"),
        "issued_at": payload.get("iat"),
        "expiration": payload.get("exp"),
        "session_id": payload.get("sid"),
        "user_id": payload.get("sub"),
    }


def get_privy_user(user_id):
    app_id = read_required_env("PRIVY_APP_ID")
    try:
        response = requests.get(
            f"{privy_api_base_url()}/v1/users/{user_id}",
            auth=(app_id, read_required_env("PRIVY_APP_SECRET")),
            headers={"Accept": "application/json", "privy-app-id": app_id, "privy-client": PRIVY_CLIENT_HEADER},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ProviderNotConfiguredError(message="privy_user_lookup_unavailable") from exc
    if response.status_code >= 400:
        raise InvalidAuthTokenError()
    return response.json()


def exchange_privy_access_token(access_token):
    verified = verify_privy_access_token(access_token)
    user = get_privy_user(verified["user_id"])
    email = pick_email(user)
    wallet_address = pick_wallet_address(user)
    smart_wallet_address = pick_smart_wallet_address(user)
    app_jwt = sign_app_jwt(
        {
            "privyUserId": verified["user_id"],
            "sessionId": verified["session_id"],
            "expiresAtSeconds": verified["expiration"],
            "email": email,
            "walletAddress": wallet_address,
            "smartWalletAddress": smart_wallet_address,
        }
    )
    result = {
        "token": app_jwt["token"],
        "expiresAt": app_jwt["expiresAt"],
        "privyUserId": verified["user_id"],
        "sessionId": verified["session_id"],
        "email": email,
        "walletAddress": wallet_address,
        "smartWalletAddress": smart_wallet_address,
    }
    return {key: value for key, value in result.items() if value is not None}
