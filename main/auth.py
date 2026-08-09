from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import string
import time
from urllib.parse import urlparse

from django.db import transaction
from django.utils import timezone as django_timezone
from eth_account import Account
from eth_account.messages import encode_defunct
import jwt
from web3 import Web3

from users.models import WalletAuthChallenge

from .errors import ApiError, InvalidAuthTokenError, ProviderNotConfiguredError


APP_JWT_ISSUER = "axios-data-card-api"
APP_JWT_AUDIENCE = "axios-data-card-client"
APP_JWT_TTL_SECONDS = 60 * 60
SIWE_CHAIN_ID = 1315
SIWE_CHALLENGE_TTL_SECONDS = 5 * 60
SIWE_STATEMENT = "Sign in to AXIOS with your wallet."
SIWE_NONCE_RE = re.compile(r"(?:^|\n)Nonce: ([A-Za-z0-9]{8,64})(?:\n|$)")


def read_required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ProviderNotConfiguredError(message=f"{name.lower()}_not_configured")
    return value


def app_auth_secret():
    secret = read_required_env("APP_AUTH_SECRET")
    if len(secret) < 32:
        raise ProviderNotConfiguredError(message="app_auth_secret_too_short")
    return secret


def base64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def normalize_address(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return Web3.to_checksum_address(value) if re.fullmatch(r"0x[a-fA-F0-9]{40}", value) else None


def normalize_siwe_origin(value):
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def utc_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sign_app_jwt(input_data):
    wallet_address = normalize_address(input_data.get("walletAddress"))
    if not wallet_address:
        raise InvalidAuthTokenError()

    now = int(time.time())
    exp = min(int(input_data["expiresAtSeconds"]), now + APP_JWT_TTL_SECONDS)
    chain_id = int(input_data.get("chainId") or SIWE_CHAIN_ID)
    payload = {
        "sub": input_data.get("subject") or f"eip155:{chain_id}:{wallet_address.lower()}",
        "sid": input_data["sessionId"],
        "walletAddress": wallet_address,
        "chainId": chain_id,
        "iss": APP_JWT_ISSUER,
        "aud": APP_JWT_AUDIENCE,
        "iat": now,
        "exp": exp,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    unsigned = f"{base64url(json.dumps(header, separators=(',', ':')).encode())}.{base64url(json.dumps(payload, separators=(',', ':')).encode())}"
    signature = hmac.new(app_auth_secret().encode(), unsigned.encode(), hashlib.sha256).digest()
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
            app_auth_secret(),
            algorithms=["HS256"],
            issuer=APP_JWT_ISSUER,
            audience=APP_JWT_AUDIENCE,
            options={"require": ["sub", "sid", "walletAddress", "chainId", "iat", "exp"]},
        )
    except ProviderNotConfiguredError:
        raise
    except Exception as exc:
        raise InvalidAuthTokenError() from exc

    wallet_address = normalize_address(payload.get("walletAddress"))
    if (
        not isinstance(payload.get("sub"), str)
        or not isinstance(payload.get("sid"), str)
        or not wallet_address
        or payload.get("chainId") != SIWE_CHAIN_ID
    ):
        raise InvalidAuthTokenError()
    return {
        "sub": payload["sub"],
        "sid": payload["sid"],
        "walletAddress": wallet_address,
        "chainId": SIWE_CHAIN_ID,
        "iss": APP_JWT_ISSUER,
        "aud": APP_JWT_AUDIENCE,
        "iat": payload.get("iat"),
        "exp": payload.get("exp"),
    }


def create_siwe_nonce():
    now = django_timezone.now()
    WalletAuthChallenge.objects.filter(expires_at__lt=now).delete()
    alphabet = string.ascii_letters + string.digits
    while True:
        nonce = "".join(secrets.choice(alphabet) for _ in range(24))
        if not WalletAuthChallenge.objects.filter(nonce=nonce).exists():
            WalletAuthChallenge.objects.create(
                nonce=nonce,
                expires_at=now + timedelta(seconds=SIWE_CHALLENGE_TTL_SECONDS),
            )
            return nonce


def create_siwe_message(*, nonce, address, chain_id, origin):
    wallet_address = normalize_address(address)
    normalized_origin = normalize_siwe_origin(origin)
    try:
        normalized_chain_id = int(chain_id)
    except (TypeError, ValueError) as exc:
        raise ApiError("siwe_chain_invalid", status_code=400) from exc
    if not wallet_address:
        raise ApiError("siwe_address_invalid", status_code=400)
    if normalized_chain_id != SIWE_CHAIN_ID:
        raise ApiError("siwe_chain_not_supported", status_code=400)
    if not normalized_origin:
        raise ApiError("siwe_origin_invalid", status_code=400)

    now = django_timezone.now()
    with transaction.atomic():
        try:
            challenge = WalletAuthChallenge.objects.select_for_update().get(nonce=nonce)
        except WalletAuthChallenge.DoesNotExist as exc:
            raise ApiError("siwe_nonce_invalid", status_code=401) from exc
        if challenge.consumed_at:
            raise ApiError("siwe_nonce_used", status_code=401)
        if challenge.expires_at <= now:
            raise ApiError("siwe_nonce_expired", status_code=401)

        domain = urlparse(normalized_origin).netloc
        if challenge.message:
            if (
                challenge.wallet_address.lower() != wallet_address.lower()
                or challenge.chain_id != normalized_chain_id
                or challenge.uri != normalized_origin
            ):
                raise ApiError("siwe_nonce_already_prepared", status_code=409)
            return challenge.message

        message = (
            f"{domain} wants you to sign in with your Ethereum account:\n"
            f"{wallet_address}\n\n"
            f"{SIWE_STATEMENT}\n\n"
            f"URI: {normalized_origin}\n"
            "Version: 1\n"
            f"Chain ID: {normalized_chain_id}\n"
            f"Nonce: {challenge.nonce}\n"
            f"Issued At: {utc_iso(now)}\n"
            f"Expiration Time: {utc_iso(challenge.expires_at)}"
        )
        challenge.wallet_address = wallet_address
        challenge.chain_id = normalized_chain_id
        challenge.domain = domain
        challenge.uri = normalized_origin
        challenge.message = message
        challenge.save(update_fields=["wallet_address", "chain_id", "domain", "uri", "message"])
        return message


def exchange_siwe_signature(*, message, signature):
    if not isinstance(message, str) or not isinstance(signature, str) or len(message) > 4096 or len(signature) > 1024:
        raise InvalidAuthTokenError()
    nonce_match = SIWE_NONCE_RE.search(message)
    if not nonce_match:
        raise InvalidAuthTokenError()

    now = django_timezone.now()
    with transaction.atomic():
        try:
            challenge = WalletAuthChallenge.objects.select_for_update().get(nonce=nonce_match.group(1))
        except WalletAuthChallenge.DoesNotExist as exc:
            raise InvalidAuthTokenError() from exc
        if challenge.consumed_at or challenge.expires_at <= now or not challenge.message:
            raise InvalidAuthTokenError()
        if not hmac.compare_digest(challenge.message, message):
            raise InvalidAuthTokenError()

        try:
            recovered_address = Account.recover_message(encode_defunct(text=message), signature=signature)
        except Exception as exc:
            raise InvalidAuthTokenError() from exc
        if recovered_address.lower() != challenge.wallet_address.lower():
            raise InvalidAuthTokenError()

        consumed = WalletAuthChallenge.objects.filter(
            nonce=challenge.nonce,
            consumed_at__isnull=True,
            expires_at__gt=now,
        ).update(consumed_at=now)
        if consumed != 1:
            raise InvalidAuthTokenError()
        session_id = secrets.token_urlsafe(24)
        app_jwt = sign_app_jwt(
            {
                "sessionId": session_id,
                "expiresAtSeconds": int(time.time()) + APP_JWT_TTL_SECONDS,
                "walletAddress": challenge.wallet_address,
                "chainId": challenge.chain_id,
            }
        )

    return {
        "token": app_jwt["token"],
        "expiresAt": app_jwt["expiresAt"],
        "subject": app_jwt["payload"]["sub"],
        "sessionId": session_id,
        "walletAddress": challenge.wallet_address,
        "chainId": challenge.chain_id,
    }
