from datetime import datetime, timezone as datetime_timezone
import re
import secrets
import threading

from django.db import transaction
from django.db.models import Q
from django.utils import timezone as django_timezone
from django.utils.dateparse import parse_datetime

from data.models import (
    AppDataField,
    AppExportLog,
    AppExportLogItem,
    AppOrder,
    AppOrderItem,
    AppOrderSellerPayout,
    AppQuote,
    AppSearchDocument,
)
from onchain.models import AppCdrVault, AppOnchainSale
from users.models import AppCareer, AppEducation, AppUser

from main.constants import (
    BATCH_SIZE,
    CDR_LICENSE_READ_CONDITION_ADDRESS,
    CDR_OWNER_WRITE_CONDITION_ADDRESS,
    DEFAULT_CAREER_STATUS,
    DEFAULT_COUNTRY,
    DEFAULT_EDUCATION_STATUS,
    MAX_PAID_FIELDS_PER_ORDER,
    PLATFORM_FEE_BPS,
    PURCHASE_CONTRACT_ADDRESS,
    SELLER_SHARE_BPS,
    VERIFICATION_REQUIRED_KINDS,
)


# SQLite allows a single writer. The dev runserver is multi-threaded, and one
# "save" fires 5 parallel POST /fields, so writes queue in-process.
_write_lock = threading.RLock()


def now_iso():
    return datetime.now(datetime_timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def nanoid(size=10):
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-"
    return "".join(secrets.choice(alphabet) for _ in range(size))


def to_dt(value):
    if isinstance(value, datetime):
        return django_timezone.make_aware(value, datetime_timezone.utc) if django_timezone.is_naive(value) else value
    parsed = parse_datetime(value) if isinstance(value, str) and value else None
    if not parsed:
        return django_timezone.now()
    return django_timezone.make_aware(parsed, datetime_timezone.utc) if django_timezone.is_naive(parsed) else parsed


def to_iso(value):
    if not value:
        return None
    dt_value = to_dt(value)
    return dt_value.astimezone(datetime_timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
                "createdAt": item.get("createdAt") or timestamp,
                "updatedAt": item.get("updatedAt") or timestamp,
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
                "createdAt": item.get("createdAt") or timestamp,
                "updatedAt": item.get("updatedAt") or timestamp,
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


def education_to_dict(row):
    return {
        "id": row.id,
        "education": row.education,
        "status": row.status,
        "sortOrder": row.sort_order,
        "createdAt": to_iso(row.created_at),
        "updatedAt": to_iso(row.updated_at),
    }


def career_to_dict(row):
    return {
        "id": row.id,
        "career": row.career,
        "startDate": row.start_date,
        "endDate": row.end_date,
        "status": row.status,
        "sortOrder": row.sort_order,
        "createdAt": to_iso(row.created_at),
        "updatedAt": to_iso(row.updated_at),
    }


def user_to_dict(user):
    return strip_none(
        {
            "id": user.id,
            "privyUserId": user.privy_user_id,
            "email": user.email,
            "walletAddress": user.wallet_address,
            "smartWalletAddress": user.smart_wallet_address,
            "name": user.name,
            "age": user.age,
            "occupation": user.occupation,
            "gender": user.gender,
            "country": user.country,
            "residence": user.residence,
            "displayName": user.display_name,
            "publicSlug": user.public_slug,
            "avatarUrl": user.avatar_url,
            "payoutAddress": user.payout_address,
            "createdAt": to_iso(user.created_at),
            "updatedAt": to_iso(user.updated_at),
        }
    )


def profile_to_dict(user):
    educations = [education_to_dict(item) for item in sorted(list(user.educations.all()), key=lambda item: item.sort_order)]
    careers = [career_to_dict(item) for item in sorted(list(user.careers.all()), key=lambda item: item.sort_order)]
    profile = {
        **user_to_dict(user),
        "educations": educations,
        "careers": careers,
    }
    profile["publicFields"] = compose_public_fields(profile)
    return strip_none(profile)


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
        "privyUserId": (existing or {}).get("privyUserId"),
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
        "publicSlug": (existing or {}).get("publicSlug") or f"{slugify_name(local_part)}-{nanoid(8).lower()}",
        "avatarUrl": (existing or {}).get("avatarUrl"),
        "payoutAddress": (existing or {}).get("payoutAddress"),
        "createdAt": (existing or {}).get("createdAt") or timestamp,
        "updatedAt": timestamp,
    }


def slugify_name(name):
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:36]
    return slug or "card"


def generate_public_slug(name):
    return f"{slugify_name(name)}-{nanoid(8).lower()}"


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


def profile_defaults(profile, has_profile=True):
    return {
        "privy_user_id": profile.get("privyUserId"),
        "email": profile.get("email", ""),
        "wallet_address": profile.get("walletAddress"),
        "smart_wallet_address": profile.get("smartWalletAddress"),
        "name": profile.get("name", ""),
        "age": int(profile.get("age") or 0),
        "occupation": profile.get("occupation", ""),
        "gender": profile.get("gender", ""),
        "country": normalize_country(profile.get("country")),
        "residence": profile.get("residence", ""),
        "display_name": profile.get("displayName", ""),
        "public_slug": profile.get("publicSlug", ""),
        "avatar_url": profile.get("avatarUrl"),
        "payout_address": profile.get("payoutAddress"),
        "has_profile": has_profile,
        "created_at": to_dt(profile.get("createdAt")),
        "updated_at": to_dt(profile.get("updatedAt")),
    }


def replace_profile_children(user, profile):
    AppEducation.objects.filter(user=user).delete()
    for index, item in enumerate(profile.get("educations") or []):
        AppEducation.objects.create(
            id=scoped_child_id(user.id, item.get("id") or f"education-{index + 1}", f"education-{index + 1}"),
            user=user,
            education=item.get("education", ""),
            status=item.get("status") or DEFAULT_EDUCATION_STATUS,
            sort_order=index,
            created_at=to_dt(item.get("createdAt") or profile.get("createdAt")),
            updated_at=to_dt(item.get("updatedAt") or profile.get("updatedAt")),
        )
    AppCareer.objects.filter(user=user).delete()
    for index, item in enumerate(profile.get("careers") or []):
        AppCareer.objects.create(
            id=scoped_child_id(user.id, item.get("id") or f"career-{index + 1}", f"career-{index + 1}"),
            user=user,
            career=item.get("career", ""),
            start_date=item.get("startDate", ""),
            end_date=item.get("endDate", ""),
            status=item.get("status") or DEFAULT_CAREER_STATUS,
            sort_order=index,
            created_at=to_dt(item.get("createdAt") or profile.get("createdAt")),
            updated_at=to_dt(item.get("updatedAt") or profile.get("updatedAt")),
        )


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


def refresh_search_document(user):
    profile = profile_to_dict(user)
    document = profile_to_search_document(profile)
    AppSearchDocument.objects.update_or_create(
        user=user,
        defaults={
            "age": document["age"],
            "gender": document["gender"],
            "country": document["country"] or DEFAULT_COUNTRY,
            "residence": document["residence"],
            "occupation": document["occupation"],
            "tags": document["tags"],
            "searchable_text": document["text"],
            "updated_at": django_timezone.now(),
        },
    )


def mask_value(kind, value):
    if not value:
        return ""
    if kind == "email":
        return value
    if kind == "mobile":
        return re.sub(r"\d(?=\d{4})", "*", value)
    if kind == "insurance":
        return "Insurance data"
    if kind == "height":
        return "*** cm"
    if kind == "weight":
        return "*** kg"
    if kind == "blood_type":
        return "**"
    if value.startswith("@"):
        return f"{value[:3]}***"
    return f"{value[:12]}..." if len(value) > 14 else f"{value[:2]}***"


def get_vault_for_field(field):
    try:
        return field.cdr_vault
    except AppCdrVault.DoesNotExist:
        return None


def field_to_dict(field):
    vault = get_vault_for_field(field)
    item = {
        "id": field.id,
        "userId": field.user_id,
        "profileId": field.user_id,
        "kind": field.kind,
        "label": field.label,
        "valuePreview": field.value_preview,
        "accessMode": field.access_mode,
        "priceCents": field.price_cents,
        "currency": field.currency,
        "requiresVerification": field.requires_verification,
        "verificationStatus": field.verification_status,
        "cdrState": field.cdr_state,
        "sellerAddress": field.seller_address,
        "createdAt": to_iso(field.created_at),
        "updatedAt": to_iso(field.updated_at),
    }
    if vault:
        item.update(
            {
                "cdrVaultUuid": vault.cdr_vault_uuid,
                "deployTxHash": vault.deploy_tx_hash,
                "cdrLicenseIpId": vault.cdr_license_ip_id,
                "cdrLicenseTermsId": vault.cdr_license_terms_id,
                "platformWallet": vault.platform_wallet,
                "ipaRecipient": vault.ipa_recipient,
                "ipaNftContract": vault.ipa_nft_contract,
                "ipaTokenId": vault.ipa_token_id,
                "ipRegistrationTxHash": vault.ip_registration_tx_hash,
                "ipaTransferTxHash": vault.ipa_transfer_tx_hash,
                "licenseConfigTxHash": vault.license_config_tx_hash,
                "licenseAttachTxHash": vault.license_attach_tx_hash,
                "cdrAllocateTxHash": vault.allocate_tx_hash,
            }
        )
    return strip_none(item)


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


def field_queryset():
    return AppDataField.objects.select_related("user").select_related("cdr_vault")


def profile_queryset():
    return AppUser.objects.filter(has_profile=True).prefetch_related("educations", "careers")


def list_profiles():
    return [profile_to_dict(user) for user in profile_queryset().order_by("id")]


def list_users():
    return [user_to_dict(user) for user in AppUser.objects.order_by("id")]


def get_user(user_id):
    user = AppUser.objects.filter(id=user_id).first()
    return user_to_dict(user) if user else None


def get_user_by_email(email):
    normalized = email.strip().lower()
    user = AppUser.objects.filter(email=normalized).order_by("-updated_at").first()
    return user_to_dict(user) if user else None


def save_user_dict(user, has_profile=None):
    existing = AppUser.objects.filter(id=user["id"]).first()
    defaults = profile_defaults(
        {
            **(user_to_dict(existing) if existing else {}),
            **user,
            "updatedAt": user.get("updatedAt") or now_iso(),
        },
        has_profile=existing.has_profile if has_profile is None and existing else bool(has_profile),
    )
    obj, _ = AppUser.objects.update_or_create(id=user["id"], defaults=defaults)
    return user_to_dict(obj)


def persist_user(user):
    with _write_lock:
        return save_user_dict(user, has_profile=None)


def upsert_user_by_email(email):
    normalized = email.strip().lower()
    existing = get_user_by_email(normalized)
    user = create_auth_user_stub(normalized, existing)
    return user_account(persist_user(user))


def connect_wallet_to_user(email, wallet_address):
    normalized_email = email.strip().lower()
    normalized_wallet = wallet_address.lower()
    for user in AppUser.objects.exclude(email=normalized_email):
        if (user.wallet_address or "").lower() == normalized_wallet and user.email:
            return "wallet_conflict"
    existing = get_user_by_email(normalized_email)
    if existing and existing.get("walletAddress") and existing["walletAddress"].lower() != normalized_wallet:
        return "wallet_locked"
    user = create_auth_user_stub(normalized_email, existing)
    user["walletAddress"] = wallet_address
    user["updatedAt"] = now_iso()
    return user_account(persist_user(user))


def get_profile(profile_id):
    user = profile_queryset().filter(id=profile_id).first()
    return profile_to_dict(user) if user else None


def get_profile_by_slug(slug):
    user = profile_queryset().filter(public_slug=slug).first()
    return profile_to_dict(user) if user else None


def save_profile(profile):
    with _write_lock:
        with transaction.atomic():
            obj, _ = AppUser.objects.update_or_create(id=profile["id"], defaults=profile_defaults(profile, has_profile=True))
            replace_profile_children(obj, profile)
            obj = profile_queryset().get(id=obj.id)
            refresh_search_document(obj)
            return profile_to_dict(obj)


def upsert_profile(body):
    existing = get_profile(body.get("id")) if body.get("id") else None
    profile = profile_from_input(body, existing)
    return save_profile(profile)


def save_field_dict(field):
    with _write_lock:
        with transaction.atomic():
            user = AppUser.objects.filter(id=field["userId"]).first()
            if not user:
                user = AppUser.objects.create(id=field["userId"], public_slug=field["userId"], display_name=field["userId"], name=field["userId"])
            obj, _ = AppDataField.objects.update_or_create(
                id=field["id"],
                defaults={
                    "user": user,
                    "kind": field["kind"],
                    "label": field["label"],
                    "value_preview": field.get("valuePreview", ""),
                    "access_mode": field.get("accessMode") or "free",
                    "price_cents": int(field.get("priceCents") or 0),
                    "currency": field.get("currency") or "IP",
                    "requires_verification": bool(field.get("requiresVerification")),
                    "verification_status": field.get("verificationStatus") or "not_required",
                    "cdr_state": field.get("cdrState") or "off",
                    "seller_address": field.get("sellerAddress"),
                    "created_at": to_dt(field.get("createdAt")),
                    "updated_at": to_dt(field.get("updatedAt")),
                },
            )
            save_cdr_vault_from_field(obj, field)
            return field_to_dict(field_queryset().get(id=obj.id))


def save_cdr_vault_from_field(field_model, field):
    if not any(
        field.get(key)
        for key in [
            "cdrVaultUuid",
            "deployTxHash",
            "cdrLicenseIpId",
            "cdrLicenseTermsId",
            "ipaNftContract",
            "ipaTokenId",
            "licenseConfigTxHash",
            "cdrAllocateTxHash",
        ]
    ):
        return None
    try:
        vault = field_model.cdr_vault
    except AppCdrVault.DoesNotExist:
        vault = AppCdrVault(field=field_model, id=f"{field_model.id}-cdr")
    vault.network = field.get("network") or "aeneid"
    vault.cdr_vault_uuid = str(field.get("cdrVaultUuid") or vault.cdr_vault_uuid or "0")
    vault.owner_address = (
        field.get("cdrOwnerAddress")
        or field.get("platformWallet")
        or vault.owner_address
        or field.get("ipaRecipient")
        or field.get("sellerAddress")
        or field_model.user.wallet_address
    )
    vault.write_condition_address = field.get("writeConditionAddress") or CDR_OWNER_WRITE_CONDITION_ADDRESS
    vault.read_condition_address = field.get("readConditionAddress") or CDR_LICENSE_READ_CONDITION_ADDRESS
    vault.write_condition_data = field.get("writeConditionData") or "0x"
    vault.read_condition_data = field.get("readConditionData")
    vault.allocate_tx_hash = field.get("cdrAllocateTxHash") or field.get("allocateTxHash") or vault.allocate_tx_hash
    vault.deploy_tx_hash = field.get("deployTxHash") or vault.deploy_tx_hash
    vault.cdr_license_ip_id = field.get("cdrLicenseIpId") or vault.cdr_license_ip_id
    vault.cdr_license_terms_id = str(field.get("cdrLicenseTermsId")) if field.get("cdrLicenseTermsId") is not None else vault.cdr_license_terms_id
    vault.platform_wallet = field.get("platformWallet") or vault.platform_wallet
    vault.ipa_recipient = field.get("ipaRecipient") or vault.ipa_recipient
    vault.ipa_nft_contract = field.get("ipaNftContract") or vault.ipa_nft_contract
    vault.ipa_token_id = str(field.get("ipaTokenId")) if field.get("ipaTokenId") is not None else vault.ipa_token_id
    vault.ip_registration_tx_hash = field.get("ipRegistrationTxHash") or vault.ip_registration_tx_hash
    vault.ipa_transfer_tx_hash = field.get("ipaTransferTxHash") or vault.ipa_transfer_tx_hash
    vault.license_config_tx_hash = field.get("licenseConfigTxHash") or vault.license_config_tx_hash
    vault.license_attach_tx_hash = field.get("licenseAttachTxHash") or vault.license_attach_tx_hash
    vault.status = "active" if field_model.cdr_state == "on" else "allocating"
    vault.updated_at = django_timezone.now()
    vault.save()
    return vault


def upsert_field(body):
    timestamp = now_iso()
    owner_id = body.get("userId") or body.get("profileId") or ""
    existing_model = None
    if body.get("id"):
        existing_model = field_queryset().filter(id=body["id"]).first()
    if not existing_model:
        existing_model = field_queryset().filter(user_id=owner_id, kind=body.get("kind")).first()
    existing = field_to_dict(existing_model) if existing_model else None
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
        "id": body.get("id") or (existing or {}).get("id") or f"field-{nanoid(10)}",
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
    return save_field_dict(field)


def mark_field_verified(field_id):
    field = field_queryset().filter(id=field_id).first()
    if not field:
        return None
    field.verification_status = "verified"
    field.updated_at = django_timezone.now()
    field.save(update_fields=["verification_status", "updated_at"])
    return field_to_dict(field_queryset().get(id=field_id))


def save_deploy_log(field_id, cdr_vault_uuid, deploy_tx_hash):
    field = field_queryset().filter(id=field_id).first()
    field_dict = field_to_dict(field) if field else None
    if not field or not field_is_deployable(field_dict):
        return None
    field.access_mode = "paid"
    field.cdr_state = "on"
    field.value_preview = mask_value(field.kind, field.value_preview)
    field.updated_at = django_timezone.now()
    field.save(update_fields=["access_mode", "cdr_state", "value_preview", "updated_at"])
    next_field = {**field_dict, "accessMode": "paid", "cdrState": "on", "cdrVaultUuid": cdr_vault_uuid, "deployTxHash": deploy_tx_hash}
    save_cdr_vault_from_field(field, next_field)
    return field_to_dict(field_queryset().get(id=field_id))


def save_server_cdr_deployment(field_id, deployment):
    field = field_queryset().filter(id=field_id).first()
    field_dict = field_to_dict(field) if field else None
    if not field or not field_can_start_cdr_deploy(field_dict):
        return None
    field.access_mode = "paid"
    field.cdr_state = "on"
    field.value_preview = mask_value(field.kind, field.value_preview)
    field.updated_at = django_timezone.now()
    field.save(update_fields=["access_mode", "cdr_state", "value_preview", "updated_at"])
    next_field = {
        **field_dict,
        "accessMode": "paid",
        "cdrState": "on",
        "cdrVaultUuid": str(deployment["cdrVaultUuid"]),
        "deployTxHash": deployment["deployTxHash"],
        "cdrLicenseIpId": deployment["cdrLicenseIpId"],
        "cdrLicenseTermsId": str(deployment["cdrLicenseTermsId"]),
        "platformWallet": deployment.get("platformWallet"),
        "ipaRecipient": deployment.get("recipient"),
        "cdrOwnerAddress": deployment.get("cdrOwnerAddress"),
        "writeConditionAddress": deployment.get("writeConditionAddress"),
        "readConditionAddress": deployment.get("readConditionAddress"),
        "writeConditionData": deployment.get("writeConditionData"),
        "readConditionData": deployment.get("readConditionData"),
        "ipaNftContract": deployment.get("ipaNftContract"),
        "ipaTokenId": str(deployment["ipaTokenId"]) if deployment.get("ipaTokenId") is not None else None,
        "ipRegistrationTxHash": deployment.get("ipRegistrationTxHash"),
        "ipaTransferTxHash": deployment.get("ipaTransferTxHash"),
        "licenseConfigTxHash": deployment.get("licenseConfigTxHash"),
        "licenseAttachTxHash": deployment.get("licenseAttachTxHash"),
        "cdrAllocateTxHash": deployment.get("allocateTxHash"),
    }
    save_cdr_vault_from_field(field, next_field)
    return field_to_dict(field_queryset().get(id=field_id))


def set_cdr_state(field_id, cdr_state):
    field = field_queryset().filter(id=field_id).first()
    field_dict = field_to_dict(field) if field else None
    if not field or (cdr_state != "off" and not field_is_deployable(field_dict)):
        return None
    if cdr_state == "on" and not field_has_mintable_license(field_dict):
        return None
    field.cdr_state = cdr_state
    if cdr_state == "on":
        field.access_mode = "paid"
    field.updated_at = django_timezone.now()
    field.save(update_fields=["cdr_state", "access_mode", "updated_at"])
    vault = get_vault_for_field(field)
    if vault:
        vault.status = "active" if cdr_state == "on" else "revoked"
        vault.updated_at = django_timezone.now()
        vault.save(update_fields=["status", "updated_at"])
    return field_to_dict(field_queryset().get(id=field_id))


def get_search_documents():
    profile_count = AppUser.objects.filter(has_profile=True).count()
    if profile_count and AppSearchDocument.objects.count() < profile_count:
        for user in profile_queryset():
            refresh_search_document(user)
    documents = []
    for row in AppSearchDocument.objects.select_related("user").filter(user__has_profile=True):
        documents.append(
            {
                "profileId": row.user_id,
                "userId": row.user_id,
                "publicSlug": row.user.public_slug,
                "gender": row.gender,
                "age": row.age,
                "country": row.country,
                "locale": row.residence,
                "residence": row.residence,
                "occupation": row.occupation,
                "tags": row.tags,
                "text": row.searchable_text,
            }
        )
    return documents


def get_fields_by_profile_ids(profile_ids):
    wanted = set(profile_ids)
    return [field_to_dict(field) for field in field_queryset().filter(user_id__in=wanted).order_by("id")]


def get_fields_by_profile_id(profile_id):
    return [field_to_dict(field) for field in field_queryset().filter(user_id=profile_id).order_by("id")]


def get_fields_by_ids(field_ids):
    wanted = list(dict.fromkeys(field_ids))
    fields = {field.id: field_to_dict(field) for field in field_queryset().filter(id__in=wanted)}
    return [fields[field_id] for field_id in wanted if field_id in fields]


def quote_to_dict(quote):
    return strip_none(
        {
            "id": quote.id,
            "buyerWallet": quote.buyer_wallet,
            "prompt": quote.prompt,
            "filters": quote.filters,
            "recommendedFields": quote.recommended_fields,
            "wantedFields": quote.wanted_fields,
            "profileIds": quote.profile_ids,
            "matches": quote.matches,
            "extensions": quote.extensions,
            "matchedProfileCount": quote.matched_profile_count,
            "paidFieldCount": quote.paid_field_count,
            "freeFieldCount": quote.free_field_count,
            "subtotalCents": quote.subtotal_cents,
            "serviceFeeCents": quote.service_fee_cents,
            "totalCents": quote.total_cents,
            "currency": quote.currency,
            "batchSize": quote.batch_size,
            "capped": quote.capped,
            "maxPaidFields": quote.max_paid_fields,
            "sheetParams": quote.sheet_params,
            "createdAt": to_iso(quote.created_at),
            "updatedAt": to_iso(quote.updated_at),
        }
    )


def save_quote(quote):
    with _write_lock:
        obj, _ = AppQuote.objects.update_or_create(
            id=quote["id"],
            defaults={
                "buyer_wallet": quote.get("buyerWallet", ""),
                "prompt": quote.get("prompt", ""),
                "filters": quote.get("filters") or {},
                "recommended_fields": quote.get("recommendedFields") or [],
                "wanted_fields": quote.get("wantedFields") or [],
                "profile_ids": quote.get("profileIds") or [],
                "matches": quote.get("matches") or [],
                "extensions": quote.get("extensions") or [],
                "matched_profile_count": int(quote.get("matchedProfileCount") or 0),
                "paid_field_count": int(quote.get("paidFieldCount") or 0),
                "free_field_count": int(quote.get("freeFieldCount") or 0),
                "subtotal_cents": int(quote.get("subtotalCents") or 0),
                "service_fee_cents": int(quote.get("serviceFeeCents") or 0),
                "total_cents": int(quote.get("totalCents") or 0),
                "currency": quote.get("currency") or "IP",
                "batch_size": int(quote.get("batchSize") or BATCH_SIZE),
                "capped": bool(quote.get("capped")),
                "max_paid_fields": int(quote.get("maxPaidFields") or MAX_PAID_FIELDS_PER_ORDER),
                "sheet_params": quote.get("sheetParams") or {},
                "created_at": to_dt(quote.get("createdAt")),
                "updated_at": to_dt(quote.get("updatedAt") or now_iso()),
            },
        )
    return quote_to_dict(obj)


def get_quote(quote_id):
    quote = AppQuote.objects.filter(id=quote_id).first()
    return quote_to_dict(quote) if quote else None


def list_quotes_by_buyer_wallet(wallet):
    normalized = wallet.lower()
    return [
        quote_to_dict(quote)
        for quote in AppQuote.objects.filter(buyer_wallet__iexact=normalized).order_by("-created_at")
    ]


def payout_to_dict(payout):
    return {
        "sellerAddress": payout.seller_address,
        "fieldIds": payout.field_ids,
        "grossCents": payout.gross_cents,
        "sellerCents": payout.seller_cents,
        "serviceFeeCents": payout.service_fee_cents,
    }


def order_to_dict(order):
    return strip_none(
        {
            "id": order.id,
            "quoteId": order.quote_id,
            "buyerWallet": order.buyer_wallet,
            "prompt": order.prompt,
            "filters": order.filters,
            "selectedProfileIds": order.selected_profile_ids,
            "selectedMatchRefs": order.selected_match_refs,
            "selectedFieldIds": order.selected_field_ids,
            "subtotalCents": order.subtotal_cents,
            "serviceFeeCents": order.service_fee_cents,
            "totalCents": order.total_cents,
            "currency": order.currency,
            "batchSize": order.batch_size,
            "platformFeeBps": order.platform_fee_bps,
            "status": order.status,
            "paymentTxHash": order.payment_tx_hash,
            "licenseTokenIds": order.license_token_ids,
            "licenseTokenGrants": order.license_token_grants,
            "purchaseContract": order.purchase_contract,
            "accessProof": order.access_proof,
            "sheetParams": order.sheet_params,
            "sellerPayouts": [payout_to_dict(payout) for payout in order.seller_payouts.all()],
            "createdAt": to_iso(order.created_at),
            "updatedAt": to_iso(order.updated_at),
        }
    )


def save_order(order):
    with _write_lock:
        with transaction.atomic():
            quote = AppQuote.objects.filter(id=order.get("quoteId")).first()
            obj, _ = AppOrder.objects.update_or_create(
                id=order["id"],
                defaults={
                    "quote": quote,
                    "buyer_wallet": order["buyerWallet"],
                    "prompt": order.get("prompt", ""),
                    "filters": order.get("filters") or {},
                    "selected_profile_ids": order.get("selectedProfileIds") or [],
                    "selected_match_refs": order.get("selectedMatchRefs") or [],
                    "selected_field_ids": order.get("selectedFieldIds") or [],
                    "subtotal_cents": int(order.get("subtotalCents") or 0),
                    "service_fee_cents": int(order.get("serviceFeeCents") or 0),
                    "total_cents": int(order.get("totalCents") or 0),
                    "currency": order.get("currency") or "IP",
                    "batch_size": int(order.get("batchSize") or BATCH_SIZE),
                    "platform_fee_bps": int(order.get("platformFeeBps") or PLATFORM_FEE_BPS),
                    "status": order.get("status") or "pending_payment",
                    "payment_tx_hash": order.get("paymentTxHash"),
                    "license_token_ids": order.get("licenseTokenIds") or [],
                    "license_token_grants": order.get("licenseTokenGrants") or [],
                    "purchase_contract": order.get("purchaseContract") or PURCHASE_CONTRACT_ADDRESS,
                    "access_proof": order.get("accessProof") or "",
                    "sheet_params": order.get("sheetParams") or {},
                    "created_at": to_dt(order.get("createdAt")),
                    "updated_at": to_dt(order.get("updatedAt")),
                },
            )
            AppOrderItem.objects.filter(order=obj).delete()
            grant_by_field = {grant.get("fieldId"): grant for grant in order.get("licenseTokenGrants") or []}
            match_by_profile = {
                profile_id: order.get("selectedMatchRefs", [])[index] if index < len(order.get("selectedMatchRefs", [])) else f"match-{index + 1}"
                for index, profile_id in enumerate(order.get("selectedProfileIds") or [])
            }
            for field_id in order.get("selectedFieldIds") or []:
                field = field_queryset().filter(id=field_id).first()
                if not field:
                    continue
                field_dict = field_to_dict(field)
                grant = grant_by_field.get(field_id) or {}
                AppOrderItem.objects.create(
                    order=obj,
                    field=field,
                    seller_user=field.user,
                    match_ref=match_by_profile.get(field.user_id, ""),
                    kind=field.kind,
                    price_cents=field.price_cents,
                    seller_address=field.seller_address or field.user.wallet_address or "",
                    cdr_vault_uuid=field_dict.get("cdrVaultUuid"),
                    license_token_id=str(grant.get("licenseTokenId")) if grant.get("licenseTokenId") is not None else None,
                    license_mint_tx_hash=grant.get("mintTxHash"),
                    created_at=to_dt(order.get("createdAt")),
                )
            AppOrderSellerPayout.objects.filter(order=obj).delete()
            for payout in order.get("sellerPayouts") or []:
                AppOrderSellerPayout.objects.create(
                    order=obj,
                    seller_address=payout.get("sellerAddress") or "",
                    field_ids=payout.get("fieldIds") or [],
                    gross_cents=int(payout.get("grossCents") or 0),
                    seller_cents=int(payout.get("sellerCents") or 0),
                    service_fee_cents=int(payout.get("serviceFeeCents") or 0),
                    created_at=to_dt(order.get("createdAt")),
                )
            return order_to_dict(AppOrder.objects.prefetch_related("seller_payouts").get(id=obj.id))


def get_order(order_id):
    order = AppOrder.objects.prefetch_related("seller_payouts").filter(id=order_id).first()
    return order_to_dict(order) if order else None


def update_order(order):
    existing = AppOrder.objects.filter(id=order["id"]).first()
    if not existing:
        return save_order(order)
    existing.status = order.get("status", existing.status)
    existing.payment_tx_hash = order.get("paymentTxHash", existing.payment_tx_hash)
    existing.updated_at = django_timezone.now()
    existing.save(update_fields=["status", "payment_tx_hash", "updated_at"])
    return order_to_dict(AppOrder.objects.prefetch_related("seller_payouts").get(id=existing.id))


def list_orders_by_buyer_wallet(wallet):
    normalized = wallet.lower()
    return [
        order_to_dict(order)
        for order in AppOrder.objects.prefetch_related("seller_payouts").filter(buyer_wallet__iexact=normalized).order_by("-created_at")
    ]


def save_export_log(order_id, payload):
    order = AppOrder.objects.filter(id=order_id).first()
    if not order:
        return None
    log = AppExportLog.objects.create(
        order=order,
        generated_at=to_dt(payload.get("generatedAt")),
        format=payload.get("format") or "csv",
        successful_field_ids=payload.get("successfulFieldIds") or [],
        failed_field_ids=payload.get("failedFieldIds") or [],
    )
    successful = set(payload.get("successfulFieldIds") or [])
    failed = set(payload.get("failedFieldIds") or [])
    for item in AppOrderItem.objects.filter(order=order):
        if item.field_id in successful or item.field_id in failed:
            AppExportLogItem.objects.create(export_log=log, order_item=item, success=item.field_id in successful)
    return {
        "id": log.id,
        "orderId": order.id,
        "generatedAt": to_iso(log.generated_at),
        "format": log.format,
        "successfulFieldIds": log.successful_field_ids,
        "failedFieldIds": log.failed_field_ids,
        "createdAt": to_iso(log.created_at),
    }


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


def sales_wallet_scope(wallet):
    normalized = (wallet or "").lower()
    aliases = {normalized}
    user_filter = Q(wallet_address__iexact=normalized) | Q(smart_wallet_address__iexact=normalized) | Q(payout_address__iexact=normalized)
    users = list(AppUser.objects.filter(user_filter))
    for user in users:
        for value in [user.wallet_address, user.smart_wallet_address, user.payout_address]:
            if value:
                aliases.add(value.lower())
    return aliases, [user.id for user in users]


def list_sales_by_wallet(wallet):
    aliases, user_ids = sales_wallet_scope(wallet)
    item_filter = Q(pk__isnull=True)
    for alias in aliases:
        item_filter |= Q(seller_address__iexact=alias)
    if user_ids:
        item_filter |= Q(seller_user_id__in=user_ids)

    sales = []
    items = AppOrderItem.objects.select_related("order", "field", "seller_user").filter(item_filter).distinct().order_by("-created_at")
    for item in items:
        field = field_to_dict(item.field)
        service_fee = round(item.price_cents * PLATFORM_FEE_BPS / 10000)
        sales.append(
            {
                "id": f"{item.order_id}-{item.field_id}",
                "orderId": item.order_id,
                "buyerWallet": item.order.buyer_wallet,
                "fieldId": item.field_id,
                "kind": item.kind,
                "label": item.field.label,
                "cdrLicenseIpId": field.get("cdrLicenseIpId"),
                "grossCents": item.price_cents,
                "sellerCents": round(item.price_cents * SELLER_SHARE_BPS / 10000),
                "serviceFeeCents": service_fee,
                "paymentTxHash": item.order.payment_tx_hash,
                "source": "server",
                "createdAt": to_iso(item.order.created_at),
            }
        )
    return sorted(sales, key=lambda item: item["createdAt"], reverse=True)


def onchain_sale_to_dict(sale):
    return strip_none(
        {
            "id": sale.id,
            "orderId": sale.order_id,
            "buyerWallet": sale.buyer_wallet,
            "fieldId": sale.field_id,
            "kind": sale.kind or (sale.field.kind if sale.field else None),
            "label": sale.label or (sale.field.label if sale.field else "Paid data"),
            "cdrLicenseIpId": sale.cdr_license_ip_id,
            "grossCents": sale.gross_cents,
            "sellerCents": sale.seller_cents,
            "serviceFeeCents": sale.service_fee_cents,
            "paymentTxHash": sale.payment_tx_hash,
            "source": sale.source,
            "blockNumber": sale.block_number,
            "logIndex": sale.log_index,
            "createdAt": to_iso(sale.created_at),
        }
    )


def save_onchain_sales(sales):
    saved = []
    for sale in sales:
        field = AppDataField.objects.filter(id=sale.get("fieldId")).first() if sale.get("fieldId") and sale.get("fieldId") != "0x" else None
        order = AppOrder.objects.filter(id=sale.get("orderId")).first() if sale.get("orderId") and not str(sale.get("orderId")).startswith("0x") else None
        obj, _ = AppOnchainSale.objects.update_or_create(
            id=sale["id"],
            defaults={
                "order": order,
                "field": field,
                "buyer_wallet": sale.get("buyerWallet") or "",
                "seller_address": sale.get("sellerAddress") or "",
                "kind": sale.get("kind") or "",
                "label": sale.get("label") or "Paid data",
                "cdr_license_ip_id": sale.get("cdrLicenseIpId"),
                "gross_cents": int(sale.get("grossCents") or 0),
                "seller_cents": int(sale.get("sellerCents") or 0),
                "service_fee_cents": int(sale.get("serviceFeeCents") or 0),
                "payment_tx_hash": sale.get("paymentTxHash") or "",
                "block_number": str(sale.get("blockNumber")) if sale.get("blockNumber") is not None else None,
                "log_index": sale.get("logIndex"),
                "source": sale.get("source") or "onchain",
                "created_at": to_dt(sale.get("createdAt")),
            },
        )
        saved.append(onchain_sale_to_dict(obj))
    return saved
