from datetime import datetime, timedelta, timezone
from pathlib import Path
import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import subprocess
import uuid

from eth_account import Account
from eth_account.messages import encode_defunct
from django.utils import timezone as django_timezone
import requests
from web3 import Web3

from data.models import AppVerification

from api.constants import MAX_PROFILE_IMAGE_BYTES, PURCHASE_CONTRACT_ADDRESS, STORY_AENEID_RPC_URL
from api.errors import ApiError, ProviderNotConfiguredError

SUPPORTED_MIME_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
FIELD_IP_METADATA_IMAGE_KEY = "story-field-assets/field-ip-card.png"

SEND_STATE = {}
MAX_CONFIRM_ATTEMPTS = 5
RESEND_MIN_INTERVAL_SECONDS = 30
SEND_WINDOW_SECONDS = 24 * 60 * 60
MAX_SENDS_PER_TARGET_PER_WINDOW = 5


def required(name, provider):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ProviderNotConfiguredError(message=f"{provider}_provider_not_configured")
    return value


def create_code():
    return str(secrets.randbelow(900000) + 100000)


def hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def verification_id():
    return "verify-" + secrets.token_hex(8)


def send_state_key(channel, target):
    return f"{channel}:{target.strip().lower()}"


def assert_send_allowed(channel, target):
    key = send_state_key(channel, target)
    now = datetime.now(timezone.utc).timestamp()
    state = SEND_STATE.get(key)
    if not state or now - state["windowStart"] > SEND_WINDOW_SECONDS:
        state = {"count": 0, "windowStart": now, "lastSentAt": 0}
    SEND_STATE[key] = state
    if state["lastSentAt"] and now - state["lastSentAt"] < RESEND_MIN_INTERVAL_SECONDS:
        raise ApiError("rate_limited", "verification_resend_too_soon", status_code=429)
    if state["count"] >= MAX_SENDS_PER_TARGET_PER_WINDOW:
        raise ApiError("rate_limited", "verification_send_limit_reached", status_code=429)


def record_send(channel, target):
    key = send_state_key(channel, target)
    state = SEND_STATE.get(key) or {"count": 0, "windowStart": datetime.now(timezone.utc).timestamp(), "lastSentAt": 0}
    state["count"] += 1
    state["lastSentAt"] = datetime.now(timezone.utc).timestamp()
    SEND_STATE[key] = state


def send_email_code(target, code):
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {required('RESEND_API_KEY', 'email')}", "Content-Type": "application/json"},
        json={
            "from": required("RESEND_FROM_EMAIL", "email"),
            "to": target,
            "subject": "AXIOS verification code",
            "text": f"Your AXIOS verification code is {code}",
        },
        timeout=15,
    )
    if response.status_code >= 400:
        raise ApiError("provider_error", f"email_send_failed:{response.status_code}", status_code=502)


def send_sms_code(target, code):
    sid = required("TWILIO_ACCOUNT_SID", "mobile")
    response = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, required("TWILIO_AUTH_TOKEN", "mobile")),
        data={"To": target, "From": required("TWILIO_FROM_NUMBER", "mobile"), "Body": f"Your AXIOS verification code is {code}"},
        timeout=15,
    )
    if response.status_code >= 400:
        raise ApiError("provider_error", f"mobile_send_failed:{response.status_code}", status_code=502)


def start_verification(channel, target):
    assert_send_allowed(channel, target)
    code = create_code()
    if channel == "email":
        send_email_code(target, code)
        provider = "resend"
    else:
        send_sms_code(target, code)
        provider = "twilio_sms"
    record_send(channel, target)
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    vid = verification_id()
    AppVerification.objects.create(
        id=vid,
        channel=channel,
        target=target,
        code_hash=hash_code(code),
        provider=provider,
        expires_at=expires,
    )
    return {"verificationId": vid, "expiresAt": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z")}


def confirm_verification(verification_id_value, code):
    record = AppVerification.objects.filter(id=verification_id_value, consumed_at__isnull=True).first()
    if not record or record.expires_at < django_timezone.now():
        return False
    expected = bytes.fromhex(record.code_hash)
    actual = bytes.fromhex(hash_code(code))
    if not hmac.compare_digest(expected, actual):
        record.attempts += 1
        if record.attempts >= MAX_CONFIRM_ATTEMPTS:
            record.delete()
        else:
            record.save(update_fields=["attempts"])
        return False
    record.consumed_at = django_timezone.now()
    record.save(update_fields=["consumed_at"])
    return True


def is_supported_image(buffer, mime_type):
    if mime_type == "image/jpeg":
        return buffer[:3] == b"\xff\xd8\xff"
    if mime_type == "image/png":
        return buffer[:8] == b"\x89PNG\r\n\x1a\n"
    if mime_type == "image/webp":
        return buffer[:4] == b"RIFF" and buffer[8:12] == b"WEBP"
    if mime_type == "image/gif":
        return buffer[:3] == b"GIF"
    return False


def decode_profile_image(data_base64, mime_type):
    if mime_type not in SUPPORTED_MIME_TYPES:
        raise ApiError("unsupported_image_type", status_code=400)
    payload = data_base64.split(",")[-1]
    buffer = base64.b64decode(payload)
    if not buffer or len(buffer) > MAX_PROFILE_IMAGE_BYTES:
        raise ApiError("invalid_image_size", status_code=400)
    if not is_supported_image(buffer, mime_type):
        raise ApiError("invalid_image_content", status_code=400)
    return buffer


def hmac_bytes(key, value):
    return hmac.new(key if isinstance(key, bytes) else key.encode(), value.encode(), hashlib.sha256).digest()


def sha256_hex(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def signing_key(secret, date_stamp, region):
    date_key = hmac_bytes("AWS4" + secret, date_stamp)
    region_key = hmac_bytes(date_key, region)
    service_key = hmac_bytes(region_key, "s3")
    return hmac_bytes(service_key, "aws4_request")


def upload_profile_image(owner_wallet, file_name, mime_type, data_base64):
    buffer = decode_profile_image(data_base64, mime_type)
    original_ext = Path(file_name or "").suffix.lower()
    extension = original_ext if original_ext in SUPPORTED_MIME_TYPES.values() else SUPPORTED_MIME_TYPES[mime_type]
    key = f"profiles/{owner_wallet.lower()}/{uuid.uuid4()}{extension}"
    return {"key": key, "url": upload_public_object(key, buffer, mime_type)}


def object_storage_base():
    endpoint = required("HETZNER_OBJECT_STORAGE_ENDPOINT", "object_storage").rstrip("/")
    bucket = required("HETZNER_OBJECT_STORAGE_BUCKET", "object_storage")
    endpoint_without_scheme = endpoint.split("://", 1)[-1]
    scheme = endpoint.split("://", 1)[0] if "://" in endpoint else "https"
    host = f"{bucket}.{endpoint_without_scheme}"
    public_base = os.environ.get("HETZNER_OBJECT_STORAGE_PUBLIC_BASE_URL", "").rstrip("/") or f"{scheme}://{host}"
    return {
        "region": required("HETZNER_OBJECT_STORAGE_REGION", "object_storage"),
        "access_key": required("HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID", "object_storage"),
        "secret_key": required("HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY", "object_storage"),
        "scheme": scheme,
        "host": host,
        "public_base": public_base,
    }


def public_object_url(key):
    encoded_path = "/".join(requests.utils.quote(part, safe="") for part in key.split("/"))
    return f"{object_storage_base()['public_base']}/{encoded_path}"


def upload_public_object(key, buffer, content_type):
    storage = object_storage_base()
    encoded_path = "/".join(requests.utils.quote(part, safe="") for part in key.split("/"))
    url = f"{storage['scheme']}://{storage['host']}/{encoded_path}"
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    payload_hash = sha256_hex(buffer)
    canonical_headers = "\n".join(
        [
            f"content-type:{content_type}",
            f"host:{storage['host']}",
            "x-amz-acl:public-read",
            f"x-amz-content-sha256:{payload_hash}",
            f"x-amz-date:{amz_date}",
            "",
        ]
    )
    signed_headers = "content-type;host;x-amz-acl;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(["PUT", f"/{encoded_path}", "", canonical_headers, signed_headers, payload_hash])
    credential_scope = f"{date_stamp}/{storage['region']}/s3/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, credential_scope, sha256_hex(canonical_request)])
    signature = hmac.new(signing_key(storage["secret_key"], date_stamp, storage["region"]), string_to_sign.encode(), hashlib.sha256).hexdigest()
    response = requests.put(
        url,
        headers={
            "Authorization": f"AWS4-HMAC-SHA256 Credential={storage['access_key']}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}",
            "Content-Type": content_type,
            "x-amz-acl": "public-read",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        },
        data=buffer,
        timeout=30,
    )
    if response.status_code >= 400:
        raise ApiError("object_storage_upload_failed", f"object_storage_upload_failed:{response.status_code}", status_code=502)
    return f"{storage['public_base']}/{encoded_path}"


def safe_object_path_part(value):
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip()).strip("-") or "field"


def upload_field_ip_metadata(profile_id, kind, label):
    name = f"{profile_id}-{kind}"
    image_url = public_object_url(FIELD_IP_METADATA_IMAGE_KEY)
    metadata = {
        "name": name,
        "description": f"AXIOS paid data field IP metadata for {name}.",
        "image": image_url,
        "attributes": [
            {"trait_type": "User ID", "value": profile_id},
            {"trait_type": "Field", "value": kind},
            {"trait_type": "Label", "value": label or kind},
        ],
        "properties": {
            "schema": "axios-field-ip-metadata-v1",
            "userId": profile_id,
            "field": kind,
        },
    }
    buffer = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
    key = f"story-field-metadata/{safe_object_path_part(profile_id)}/{safe_object_path_part(kind)}.json"
    metadata_url = upload_public_object(key, buffer, "application/json")
    metadata_hash = f"0x{sha256_hex(buffer)}"
    return {
        "name": name,
        "imageUrl": image_url,
        "metadataUrl": metadata_url,
        "metadataHash": metadata_hash,
        "ipMetadata": {
            "ipMetadataURI": metadata_url,
            "ipMetadataHash": metadata_hash,
            "nftMetadataURI": metadata_url,
            "nftMetadataHash": metadata_hash,
        },
    }


def deploy_field_cdr_with_server_wallet(payload):
    base_dir = Path(__file__).resolve().parents[2]
    front_dir = base_dir / "front"
    script_path = front_dir / "scripts" / "server_deploy_field_cdr.mjs"
    if not script_path.exists():
        raise ProviderNotConfiguredError(message="server_cdr_deploy_script_missing")

    try:
        completed = subprocess.run(
            ["node", str(script_path)],
            cwd=front_dir,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=240,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ApiError("server_cdr_deploy_timeout", status_code=504) from exc
    except OSError as exc:
        raise ProviderNotConfiguredError(message="node_runtime_not_configured") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise ApiError("server_cdr_deploy_failed", detail[:500] or "server_cdr_deploy_failed", status_code=502)

    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise ApiError("server_cdr_deploy_invalid_response", status_code=502) from exc


def create_wallet_link_proof(email, user_id, wallet_address):
    private_key = os.environ.get("AXIOS_LINK_SIGNER_PRIVATE_KEY", "").strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{64}", private_key):
        raise ProviderNotConfiguredError(message="wallet_link_signer_not_configured")
    issued_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    message = "\n".join(
        [
            "AXIOS Wallet Link",
            f"Email: {email}",
            f"Wallet: {wallet_address}",
            f"User: {user_id}",
            f"Issued at: {issued_at}",
            "This wallet link is immutable in the MVP.",
        ]
    )
    account = Account.from_key(private_key)
    signed = account.sign_message(encode_defunct(text=message))
    return {"signerAddress": account.address, "message": message, "signature": signed.signature.hex(), "issuedAt": issued_at}


def hex_with_0x(value):
    text = value.hex() if hasattr(value, "hex") else str(value)
    return text if text.startswith("0x") else f"0x{text}"


def list_onchain_sales_by_wallet(wallet):
    web3 = Web3(Web3.HTTPProvider(STORY_AENEID_RPC_URL, request_kwargs={"timeout": 12}))
    latest = web3.eth.block_number
    from_block = max(0, latest - 200000)
    event_topic = hex_with_0x(Web3.keccak(text="DataSaleRecorded(bytes32,address,address,bytes32,string,uint256,uint256,uint256)"))
    seller_topic = "0x" + "0" * 24 + wallet.lower().replace("0x", "")
    logs = web3.eth.get_logs(
        {
            "fromBlock": from_block,
            "toBlock": "latest",
            "address": Web3.to_checksum_address(PURCHASE_CONTRACT_ADDRESS),
            "topics": [event_topic, None, seller_topic],
        }
    )
    sales = []
    for log in logs:
        block = web3.eth.get_block(log["blockNumber"])
        created_at = datetime.fromtimestamp(block["timestamp"], timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        topics = [hex_with_0x(topic) for topic in log["topics"]]
        sales.append(
            {
                "id": f"{hex_with_0x(log['transactionHash'])}-{log['logIndex']}",
                "orderId": topics[1] if len(topics) > 1 else "0x",
                "buyerWallet": "0x" + topics[3][-40:] if len(topics) > 3 else "0x0000000000000000000000000000000000000000",
                "sellerAddress": wallet,
                "fieldId": "0x",
                "label": "Paid data",
                "grossCents": 0,
                "sellerCents": 0,
                "serviceFeeCents": 0,
                "paymentTxHash": hex_with_0x(log["transactionHash"]),
                "source": "onchain",
                "blockNumber": str(log["blockNumber"]),
                "logIndex": log["logIndex"],
                "createdAt": created_at,
            }
        )
    from . import repository

    return repository.save_onchain_sales(sales)
