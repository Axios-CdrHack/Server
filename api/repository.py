from copy import deepcopy
from datetime import datetime, timezone
import re
import secrets
import threading

from django.db import transaction

# SQLite allows a single writer. The dev runserver is multi-threaded, and one
# "save" fires 5 parallel POST /fields, so update_or_create's read->write upgrade
# races and throws "database is locked" (busy_timeout does not retry the WAL
# snapshot conflict). Serialize all writes in-process so they queue instead.
_write_lock = threading.RLock()

from .constants import (
    BATCH_SIZE,
    DATA_FIELD_KINDS,
    DEFAULT_CAREER_STATUS,
    DEFAULT_COUNTRY,
    DEFAULT_EDUCATION_STATUS,
    MAX_PAID_FIELDS_PER_ORDER,
    PLATFORM_FEE_BPS,
    PURCHASE_CONTRACT_ADDRESS,
    SELLER_SHARE_BPS,
    VERIFICATION_REQUIRED_KINDS,
)
from .models import StoreRecord
from .seed_data import build_seed


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def nanoid(size=10):
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
    return "".join(secrets.choice(alphabet) for _ in range(size))


def store_values(namespace):
    return [deepcopy(row.value) for row in StoreRecord.objects.filter(namespace=namespace)]


def store_map(namespace):
    return {row.key: deepcopy(row.value) for row in StoreRecord.objects.filter(namespace=namespace)}


def save_record(namespace, key, value):
    with _write_lock:
        StoreRecord.objects.update_or_create(namespace=namespace, key=key, defaults={"value": strip_none(value)})


def replace_store(namespace, records, key_fn):
    with _write_lock:
        StoreRecord.objects.filter(namespace=namespace).delete()
        StoreRecord.objects.bulk_create(
            [StoreRecord(namespace=namespace, key=key_fn(record), value=strip_none(record)) for record in records],
        )


def strip_none(value):
    if isinstance(value, dict):
        return {key: strip_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [strip_none(item) for item in value]
    return value


def normalize_country(value):
    return (value or "").strip() or DEFAULT_COUNTRY


def scoped_child_id(user_id, local_id, prefix):
    local = local_id or prefix
    return local if local.startswith(f"{user_id}-") else f"{user_id}-{local}"


def calculate_career_work_years(career):
    start = parse_month(career.get("startDate", ""))
    if not start:
        return 0
    end = parse_month(career.get("endDate", ""))
    if not end and career.get("status") == DEFAULT_CAREER_STATUS:
        today = datetime.now()
        end = (today.year, today.month)
    if not end:
        return 0
    months = (end[0] - start[0]) * 12 + (end[1] - start[1])
    return max(0, months // 12)


def parse_month(value):
    try:
        year_text, month_text = value.split("-", 1)
        year = int(year_text)
        month = int(month_text)
        if 1 <= month <= 12:
            return year, month
    except Exception:
        return None
    return None


def normalize_educations(user_id, source):
    items = source or [{"id": "education-1", "education": "", "status": DEFAULT_EDUCATION_STATUS}]
    result = []
    timestamp = now_iso()
    for index, item in enumerate(items):
        result.append(
            {
                "id": scoped_child_id(user_id, item.get("id") or f"education-{index + 1}", f"education-{index + 1}"),
                "education": item.get("education", ""),
                "status": item.get("status") or DEFAULT_EDUCATION_STATUS,
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }
        )
    return result


def normalize_careers(user_id, source):
    items = source or [{"id": "career-1", "career": "", "startDate": "", "endDate": "", "status": DEFAULT_CAREER_STATUS}]
    result = []
    timestamp = now_iso()
    for index, item in enumerate(items):
        status = item.get("status") or (DEFAULT_CAREER_STATUS if not item.get("endDate") else "left")
        result.append(
            {
                "id": scoped_child_id(user_id, item.get("id") or f"career-{index + 1}", f"career-{index + 1}"),
                "career": item.get("career", ""),
                "startDate": item.get("startDate", ""),
                "endDate": "" if status == DEFAULT_CAREER_STATUS else item.get("endDate", ""),
                "status": status,
                "createdAt": timestamp,
                "updatedAt": timestamp,
            }
        )
    return result


def compose_public_fields(profile):
    educations = profile.get("educations") or []
    careers = profile.get("careers") or []
    primary_education = next((item for item in educations if item.get("education") or item.get("status")), None) or {
        "id": "education-1",
        "education": "",
        "status": DEFAULT_EDUCATION_STATUS,
    }
    primary_career = next((item for item in careers if item.get("career") or item.get("startDate") or item.get("endDate") or item.get("status")), None) or {
        "id": "career-1",
        "career": "",
        "startDate": "",
        "endDate": "",
        "status": DEFAULT_CAREER_STATUS,
    }
    return {
        "name": profile.get("name", ""),
        "gender": profile.get("gender", ""),
        "age": int(profile.get("age") or 0),
        "country": normalize_country(profile.get("country")),
        "locale": profile.get("residence", ""),
        "occupation": profile.get("occupation", ""),
        "education": primary_education.get("education", ""),
        "educationStatus": primary_education.get("status") or DEFAULT_EDUCATION_STATUS,
        "educations": educations,
        "career": primary_career.get("career", ""),
        "careerWorkYears": sum(calculate_career_work_years(item) for item in careers),
        "careerStartDate": primary_career.get("startDate", ""),
        "careerEndDate": primary_career.get("endDate", ""),
        "careerStatus": primary_career.get("status") or DEFAULT_CAREER_STATUS,
        "careers": careers,
    }


def profile_from_input(body, existing=None):
    fields = body.get("publicFields") or {}
    timestamp = now_iso()
    profile_id = body.get("id") or (existing or {}).get("id") or f"user-{nanoid(10)}"
    educations = normalize_educations(profile_id, body.get("educations") or fields.get("educations"))
    careers = normalize_careers(profile_id, body.get("careers") or fields.get("careers"))
    profile = {
        "id": profile_id,
        "privyUserId": body.get("privyUserId") or (existing or {}).get("privyUserId"),
        "email": (body.get("email") or (existing or {}).get("email") or "").strip().lower(),
        "publicSlug": body.get("publicSlug") or (existing or {}).get("publicSlug") or generate_public_slug(fields.get("name") or body.get("displayName") or "card"),
        "displayName": body.get("displayName") or (existing or {}).get("displayName") or fields.get("name") or "",
        "avatarUrl": body.get("avatarUrl") or (existing or {}).get("avatarUrl"),
        "walletAddress": body.get("walletAddress") or (existing or {}).get("walletAddress"),
        "smartWalletAddress": body.get("smartWalletAddress") or (existing or {}).get("smartWalletAddress"),
        "payoutAddress": body.get("payoutAddress") or (existing or {}).get("payoutAddress") or body.get("walletAddress"),
        "name": body.get("name") or fields.get("name") or (existing or {}).get("name") or body.get("displayName") or "",
        "age": int(body.get("age") if body.get("age") is not None else fields.get("age") or (existing or {}).get("age") or 0),
        "occupation": body.get("occupation") or fields.get("occupation") or (existing or {}).get("occupation") or "",
        "gender": body.get("gender") or fields.get("gender") or (existing or {}).get("gender") or "",
        "country": normalize_country(body.get("country") or fields.get("country") or (existing or {}).get("country")),
        "residence": body.get("residence") or fields.get("locale") or (existing or {}).get("residence") or "",
        "educations": educations,
        "careers": careers,
        "createdAt": (existing or {}).get("createdAt") or timestamp,
        "updatedAt": timestamp,
    }
    profile["publicFields"] = compose_public_fields(profile)
    return profile


def user_from_profile(profile):
    return {
        "id": profile["id"],
        "privyUserId": profile.get("privyUserId"),
        "email": profile.get("email", ""),
        "walletAddress": profile.get("walletAddress"),
        "smartWalletAddress": profile.get("smartWalletAddress"),
        "name": profile.get("name", ""),
        "age": profile.get("age", 0),
        "occupation": profile.get("occupation", ""),
        "gender": profile.get("gender", ""),
        "country": profile.get("country", DEFAULT_COUNTRY),
        "residence": profile.get("residence", ""),
        "displayName": profile.get("displayName", ""),
        "publicSlug": profile.get("publicSlug", ""),
        "avatarUrl": profile.get("avatarUrl"),
        "payoutAddress": profile.get("payoutAddress"),
        "createdAt": profile.get("createdAt"),
        "updatedAt": profile.get("updatedAt"),
    }


def user_account(user):
    return strip_none(
        {
            "id": user["id"],
            "privyUserId": user.get("privyUserId"),
            "email": user.get("email", ""),
            "walletAddress": user.get("walletAddress"),
            "createdAt": user.get("createdAt"),
            "updatedAt": user.get("updatedAt"),
        }
    )


def create_auth_user_stub(email, existing=None):
    timestamp = now_iso()
    local_part = (email.split("@", 1)[0] if email else "user") or "user"
    return {
        "id": (existing or {}).get("id") or f"user-{nanoid(10)}",
        "email": email,
        "walletAddress": (existing or {}).get("walletAddress"),
        "smartWalletAddress": (existing or {}).get("smartWalletAddress"),
        "name": (existing or {}).get("name") or local_part,
        "age": (existing or {}).get("age") or 0,
        "occupation": (existing or {}).get("occupation") or "",
        "gender": (existing or {}).get("gender") or "",
        "country": normalize_country((existing or {}).get("country")),
        "residence": (existing or {}).get("residence") or "",
        "displayName": (existing or {}).get("displayName") or local_part,
        "publicSlug": (existing or {}).get("publicSlug") or "",
        "avatarUrl": (existing or {}).get("avatarUrl"),
        "payoutAddress": (existing or {}).get("payoutAddress"),
        "createdAt": (existing or {}).get("createdAt") or timestamp,
        "updatedAt": timestamp,
    }


def slugify_name(name):
    slug = re.sub(r"[^a-z0-9가-힣]+", "-", (name or "").lower()).strip("-")[:36]
    return slug or "card"


def generate_public_slug(name):
    return f"{slugify_name(name)}-{nanoid(8).lower()}"


def mask_value(kind, value):
    if not value:
        return ""
    if kind == "email":
        return value
    if kind == "mobile":
        return re.sub(r"\d(?=\d{4})", "*", value)
    if value.startswith("@"):
        return f"{value[:3]}***"
    return f"{value[:12]}..." if len(value) > 14 else f"{value[:2]}***"


def is_paid_cdr_field(field):
    return field.get("accessMode") == "paid" and field.get("cdrState") == "on"


def field_is_deployable(field):
    return bool(field.get("cdrLicenseIpId") and field.get("cdrLicenseTermsId")) and (
        not field.get("requiresVerification") or field.get("verificationStatus") == "verified"
    )


def field_has_mintable_license(field):
    return bool(
        field
        and field.get("cdrVaultUuid")
        and field.get("deployTxHash")
        and field.get("cdrLicenseIpId")
        and field.get("cdrLicenseTermsId")
        and field.get("licenseConfigTxHash")
        and field.get("ipaNftContract")
        and field.get("ipaTokenId")
    )


def field_can_start_cdr_deploy(field):
    return bool(field and field.get("valuePreview")) and (
        not field.get("requiresVerification") or field.get("verificationStatus") == "verified"
    )


def field_with_alias(field):
    item = deepcopy(field)
    item["profileId"] = item.get("userId")
    return strip_none(item)


@transaction.atomic
def ensure_seeded():
    if StoreRecord.objects.filter(namespace="profiles").exists():
        return
    users, profiles, fields = build_seed()
    replace_store("users", users, lambda item: item["id"])
    replace_store("profiles", profiles, lambda item: item["id"])
    replace_store("data_fields", fields, lambda item: item["id"])
    replace_store("orders", [], lambda item: item["id"])
    replace_store("quotes", [], lambda item: item["id"])


def list_profiles():
    ensure_seeded()
    return store_values("profiles")


def list_users():
    ensure_seeded()
    return store_values("users")


def get_user(user_id):
    ensure_seeded()
    return store_map("users").get(user_id)


def get_user_by_email(email):
    normalized = email.strip().lower()
    return next((user for user in list_users() if user.get("email") == normalized), None)


def persist_user(user):
    save_record("users", user["id"], user)


def upsert_user_by_email(email):
    normalized = email.strip().lower()
    existing = get_user_by_email(normalized)
    user = create_auth_user_stub(normalized, existing)
    persist_user(user)
    return user_account(user)


def connect_wallet_to_user(email, wallet_address):
    normalized_email = email.strip().lower()
    normalized_wallet = wallet_address.lower()
    for user in list_users():
        if user.get("walletAddress", "").lower() == normalized_wallet and user.get("email") and user.get("email") != normalized_email:
            return "wallet_conflict"
    existing = get_user_by_email(normalized_email)
    if existing and existing.get("walletAddress") and existing["walletAddress"].lower() != normalized_wallet:
        return "wallet_locked"
    user = create_auth_user_stub(normalized_email, existing)
    user["walletAddress"] = wallet_address
    user["updatedAt"] = now_iso()
    persist_user(user)
    return user_account(user)


def get_profile(profile_id):
    ensure_seeded()
    return store_map("profiles").get(profile_id)


def get_profile_by_slug(slug):
    return next((profile for profile in list_profiles() if profile.get("publicSlug") == slug), None)


def upsert_profile(body):
    ensure_seeded()
    existing = get_profile(body.get("id")) if body.get("id") else None
    profile = profile_from_input(body, existing)
    save_record("profiles", profile["id"], profile)
    save_record("users", profile["id"], user_from_profile(profile))
    return strip_none(profile)


def upsert_field(body):
    ensure_seeded()
    timestamp = now_iso()
    owner_id = body.get("userId") or body.get("profileId") or ""
    fields = store_values("data_fields")
    existing_index = next(
        (
            index
            for index, field in enumerate(fields)
            if (body.get("id") and field["id"] == body["id"]) or (not body.get("id") and field.get("userId") == owner_id and field.get("kind") == body.get("kind"))
        ),
        -1,
    )
    existing = fields[existing_index] if existing_index >= 0 else None
    requires_verification = body.get("kind") in VERIFICATION_REQUIRED_KINDS
    verification_status = body.get("verificationStatus") or (existing or {}).get("verificationStatus") or ("pending" if requires_verification else "not_required")
    if not requires_verification:
        verification_status = "not_required"
    requested_cdr_state = "off" if requires_verification and verification_status != "verified" else body.get("cdrState", "off")
    can_preserve = bool((existing or {}).get("cdrVaultUuid") and (existing or {}).get("deployTxHash"))
    cdr_state = "on" if requested_cdr_state == "on" and can_preserve else "off"
    access_mode = body.get("accessMode") or (existing or {}).get("accessMode") or "paid"
    if cdr_state == "on":
        access_mode = "paid"
    value_preview = body.get("valuePreview", "")
    if access_mode == "paid" and cdr_state == "on":
        value_preview = mask_value(body["kind"], value_preview)
    field = {
        "id": body.get("id") or f"field-{nanoid(10)}",
        "userId": owner_id,
        "profileId": owner_id,
        "kind": body["kind"],
        "label": body["label"],
        "valuePreview": value_preview,
        "accessMode": access_mode,
        "priceCents": int(body.get("priceCents") or 0),
        "currency": "IP",
        "requiresVerification": requires_verification,
        "verificationStatus": verification_status,
        "cdrState": cdr_state,
        "cdrVaultUuid": body.get("cdrVaultUuid") or (existing or {}).get("cdrVaultUuid"),
        "deployTxHash": body.get("deployTxHash") or (existing or {}).get("deployTxHash"),
        "cdrLicenseIpId": body.get("cdrLicenseIpId") or (existing or {}).get("cdrLicenseIpId"),
        "cdrLicenseTermsId": body.get("cdrLicenseTermsId") or (existing or {}).get("cdrLicenseTermsId"),
        "platformWallet": body.get("platformWallet") or (existing or {}).get("platformWallet"),
        "ipaRecipient": body.get("ipaRecipient") or (existing or {}).get("ipaRecipient"),
        "ipaNftContract": body.get("ipaNftContract") or (existing or {}).get("ipaNftContract"),
        "ipaTokenId": body.get("ipaTokenId") or (existing or {}).get("ipaTokenId"),
        "ipRegistrationTxHash": body.get("ipRegistrationTxHash") or (existing or {}).get("ipRegistrationTxHash"),
        "ipaTransferTxHash": body.get("ipaTransferTxHash") or (existing or {}).get("ipaTransferTxHash"),
        "licenseConfigTxHash": body.get("licenseConfigTxHash") or (existing or {}).get("licenseConfigTxHash"),
        "licenseAttachTxHash": body.get("licenseAttachTxHash") or (existing or {}).get("licenseAttachTxHash"),
        "cdrAllocateTxHash": body.get("cdrAllocateTxHash") or (existing or {}).get("cdrAllocateTxHash"),
        "sellerAddress": body.get("sellerAddress") or None,
        "createdAt": (existing or {}).get("createdAt") or timestamp,
        "updatedAt": timestamp,
    }
    save_record("data_fields", field["id"], field)
    return field_with_alias(field)


def mark_field_verified(field_id):
    field = get_fields_by_ids([field_id])[0] if get_fields_by_ids([field_id]) else None
    if not field:
        return None
    field["verificationStatus"] = "verified"
    field["updatedAt"] = now_iso()
    save_record("data_fields", field["id"], field)
    return field_with_alias(field)


def save_deploy_log(field_id, cdr_vault_uuid, deploy_tx_hash):
    fields = get_fields_by_ids([field_id])
    field = fields[0] if fields else None
    if not field or not field_is_deployable(field):
        return None
    field["accessMode"] = "paid"
    field["cdrState"] = "on"
    field["cdrVaultUuid"] = cdr_vault_uuid
    field["deployTxHash"] = deploy_tx_hash
    field["valuePreview"] = mask_value(field["kind"], field.get("valuePreview", ""))
    field["updatedAt"] = now_iso()
    save_record("data_fields", field["id"], field)
    return field_with_alias(field)


def save_server_cdr_deployment(field_id, deployment):
    fields = get_fields_by_ids([field_id])
    field = fields[0] if fields else None
    if not field or not field_can_start_cdr_deploy(field):
        return None
    field["accessMode"] = "paid"
    field["cdrState"] = "on"
    field["cdrVaultUuid"] = str(deployment["cdrVaultUuid"])
    field["deployTxHash"] = deployment["deployTxHash"]
    field["cdrLicenseIpId"] = deployment["cdrLicenseIpId"]
    field["cdrLicenseTermsId"] = str(deployment["cdrLicenseTermsId"])
    field["platformWallet"] = deployment.get("platformWallet")
    field["ipaRecipient"] = deployment.get("recipient")
    field["ipaNftContract"] = deployment.get("ipaNftContract")
    field["ipaTokenId"] = str(deployment["ipaTokenId"]) if deployment.get("ipaTokenId") is not None else None
    field["ipRegistrationTxHash"] = deployment.get("ipRegistrationTxHash")
    field["ipaTransferTxHash"] = deployment.get("ipaTransferTxHash")
    field["licenseConfigTxHash"] = deployment.get("licenseConfigTxHash")
    field["licenseAttachTxHash"] = deployment.get("licenseAttachTxHash")
    field["cdrAllocateTxHash"] = deployment.get("allocateTxHash")
    field["valuePreview"] = mask_value(field["kind"], field.get("valuePreview", ""))
    field["updatedAt"] = now_iso()
    save_record("data_fields", field["id"], field)
    return field_with_alias(field)


def set_cdr_state(field_id, cdr_state):
    fields = get_fields_by_ids([field_id])
    field = fields[0] if fields else None
    if not field or (cdr_state != "off" and not field_is_deployable(field)):
        return None
    if cdr_state == "on" and not field_has_mintable_license(field):
        return None
    field["cdrState"] = cdr_state
    if cdr_state == "on":
        field["accessMode"] = "paid"
    field["updatedAt"] = now_iso()
    save_record("data_fields", field["id"], field)
    return field_with_alias(field)


def profile_to_search_document(profile):
    fields = profile.get("publicFields") or {}
    educations = fields.get("educations") or []
    careers = fields.get("careers") or []
    tags = [
        fields.get("gender", ""),
        fields.get("country", ""),
        fields.get("locale", ""),
        fields.get("occupation", ""),
        *[part for item in educations for part in [item.get("education", ""), item.get("status", "")]],
        *[part for item in careers for part in [item.get("career", ""), item.get("startDate", ""), item.get("endDate", ""), item.get("status", "")]],
        *fields.get("occupation", "").lower().split(),
    ]
    tags = [item for item in tags if item]
    text = " ".join([str(fields.get(key, "")) for key in ["gender", "age", "country", "locale", "occupation"]] + tags).lower()
    return {
        "profileId": profile["id"],
        "userId": profile["id"],
        "publicSlug": profile.get("publicSlug", ""),
        "gender": fields.get("gender", ""),
        "age": int(fields.get("age") or 0),
        "country": fields.get("country", ""),
        "locale": fields.get("locale", ""),
        "residence": fields.get("locale", ""),
        "occupation": fields.get("occupation", ""),
        "tags": tags,
        "text": text,
    }


def get_search_documents():
    return [profile_to_search_document(profile) for profile in list_profiles()]


def get_fields_by_profile_ids(profile_ids):
    wanted = set(profile_ids)
    return [field_with_alias(field) for field in store_values("data_fields") if field.get("userId") in wanted]


def get_fields_by_profile_id(profile_id):
    return [field_with_alias(field) for field in store_values("data_fields") if field.get("userId") == profile_id]


def get_fields_by_ids(field_ids):
    wanted = set(field_ids)
    return [field_with_alias(field) for field in store_values("data_fields") if field.get("id") in wanted]


def save_quote(quote):
    save_record("quotes", quote["id"], quote)
    return quote


def get_quote(quote_id):
    ensure_seeded()
    return store_map("quotes").get(quote_id)


def list_quotes_by_buyer_wallet(wallet):
    normalized = wallet.lower()
    return sorted(
        [quote for quote in store_values("quotes") if quote.get("buyerWallet", "").lower() == normalized],
        key=lambda item: item.get("createdAt", ""),
        reverse=True,
    )


def save_order(order):
    save_record("orders", order["id"], order)
    return order


def get_order(order_id):
    ensure_seeded()
    return store_map("orders").get(order_id)


def update_order(order):
    order = {**order, "updatedAt": now_iso()}
    save_record("orders", order["id"], order)
    return order


def list_orders_by_buyer_wallet(wallet):
    normalized = wallet.lower()
    return sorted(
        [order for order in store_values("orders") if order.get("buyerWallet", "").lower() == normalized],
        key=lambda item: item.get("createdAt", ""),
        reverse=True,
    )


def get_public_card(slug):
    profile = get_profile_by_slug(slug)
    if not profile:
        return None
    fields = []
    for field in get_fields_by_profile_id(profile["id"]):
        item = {
            "kind": field["kind"],
            "label": field["label"],
            "accessMode": field["accessMode"],
            "priceCents": field["priceCents"],
            "currency": field["currency"],
            "cdrState": field["cdrState"],
            "verificationStatus": field["verificationStatus"],
        }
        item["valuePreview"] = field.get("valuePreview", "")
        fields.append(item)
    return {
        "id": profile["id"],
        "publicSlug": profile["publicSlug"],
        "displayName": profile["displayName"],
        "avatarUrl": profile.get("avatarUrl"),
        "publicFields": profile["publicFields"],
        "dataFields": fields,
    }


def list_sales_by_wallet(wallet):
    normalized = wallet.lower()
    sales = []
    for order in store_values("orders"):
        for field in get_fields_by_ids(order.get("selectedFieldIds", [])):
            if field.get("sellerAddress", "").lower() != normalized:
                continue
            service_fee = round(field["priceCents"] * PLATFORM_FEE_BPS / 10000)
            sales.append(
                {
                    "id": f"{order['id']}-{field['id']}",
                    "orderId": order["id"],
                    "buyerWallet": order["buyerWallet"],
                    "fieldId": field["id"],
                    "kind": field["kind"],
                    "label": field["label"],
                    "cdrLicenseIpId": field.get("cdrLicenseIpId"),
                    "grossCents": field["priceCents"],
                    "sellerCents": round(field["priceCents"] * SELLER_SHARE_BPS / 10000),
                    "serviceFeeCents": service_fee,
                    "paymentTxHash": order.get("paymentTxHash"),
                    "source": "server",
                    "createdAt": order["createdAt"],
                }
            )
    return sorted(sales, key=lambda item: item["createdAt"], reverse=True)
