from django.db import migrations
from django.utils import timezone
from django.utils.dateparse import parse_datetime


def camel(source, key, default=None):
    return source.get(key, default) if isinstance(source, dict) else default


def as_dt(value):
    parsed = parse_datetime(value) if isinstance(value, str) and value else None
    if not parsed:
        return timezone.now()
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.utc)
    return parsed


def scoped_child_id(user_id, local_id, fallback):
    local = local_id or fallback
    return local if str(local).startswith(f"{user_id}-") else f"{user_id}-{local}"


def store_map(StoreRecord, namespace):
    return {row.key: row.value for row in StoreRecord.objects.filter(namespace=namespace)}


def store_values(StoreRecord, namespace):
    return [row.value for row in StoreRecord.objects.filter(namespace=namespace)]


def public_fields_for(profile):
    fields = profile.get("publicFields") or {}
    educations = profile.get("educations") or fields.get("educations") or []
    careers = profile.get("careers") or fields.get("careers") or []
    primary_education = next((item for item in educations if item.get("education") or item.get("status")), None) or {}
    primary_career = next((item for item in careers if item.get("career") or item.get("status")), None) or {}
    result = {
        "name": profile.get("name") or fields.get("name") or "",
        "gender": profile.get("gender") or fields.get("gender") or "",
        "age": int(profile.get("age") or fields.get("age") or 0),
        "country": profile.get("country") or fields.get("country") or "Korea",
        "locale": profile.get("residence") or fields.get("locale") or "",
        "occupation": profile.get("occupation") or fields.get("occupation") or "",
        "education": primary_education.get("education") or fields.get("education") or "",
        "educationStatus": primary_education.get("status") or fields.get("educationStatus") or "graduated",
        "educations": educations,
        "career": primary_career.get("career") or fields.get("career") or "",
        "careerWorkYears": int(fields.get("careerWorkYears") or 0),
        "careerStartDate": primary_career.get("startDate") or fields.get("careerStartDate") or "",
        "careerEndDate": primary_career.get("endDate") or fields.get("careerEndDate") or "",
        "careerStatus": primary_career.get("status") or fields.get("careerStatus") or "employed",
        "careers": careers,
    }
    return result


def create_search_document(AppSearchDocument, user, profile):
    fields = public_fields_for(profile)
    tags = [
        fields.get("gender", ""),
        fields.get("country", ""),
        fields.get("locale", ""),
        fields.get("occupation", ""),
        *[part for item in fields.get("educations", []) for part in [item.get("education", ""), item.get("status", "")]],
        *[part for item in fields.get("careers", []) for part in [item.get("career", ""), item.get("startDate", ""), item.get("endDate", ""), item.get("status", "")]],
        *fields.get("occupation", "").lower().split(),
    ]
    tags = [item for item in tags if item]
    searchable_text = " ".join(
        [str(fields.get(key, "")) for key in ["gender", "age", "country", "locale", "occupation"]] + tags
    ).lower()
    AppSearchDocument.objects.update_or_create(
        user=user,
        defaults={
            "age": int(fields.get("age") or 0),
            "gender": fields.get("gender", ""),
            "country": fields.get("country") or "Korea",
            "residence": fields.get("locale", ""),
            "occupation": fields.get("occupation", ""),
            "tags": tags,
            "searchable_text": searchable_text,
            "updated_at": user.updated_at,
        },
    )


def migrate_store_records(apps, _schema_editor):
    StoreRecord = apps.get_model("api", "StoreRecord")
    AppUser = apps.get_model("users", "AppUser")
    AppEducation = apps.get_model("users", "AppEducation")
    AppCareer = apps.get_model("users", "AppCareer")
    AppDataField = apps.get_model("data", "AppDataField")
    AppSearchDocument = apps.get_model("data", "AppSearchDocument")
    AppQuote = apps.get_model("data", "AppQuote")
    AppOrder = apps.get_model("data", "AppOrder")
    AppOrderItem = apps.get_model("data", "AppOrderItem")
    AppOrderSellerPayout = apps.get_model("data", "AppOrderSellerPayout")
    AppCdrVault = apps.get_model("onchain", "AppCdrVault")
    AppOnchainSale = apps.get_model("onchain", "AppOnchainSale")

    users_by_id = store_map(StoreRecord, "users")
    profiles_by_id = store_map(StoreRecord, "profiles")
    merged_user_ids = sorted(set(users_by_id) | set(profiles_by_id))

    for user_id in merged_user_ids:
        source_user = users_by_id.get(user_id) or {}
        profile = profiles_by_id.get(user_id) or {}
        source = {**source_user, **profile}
        fields = public_fields_for(source)
        created_at = as_dt(source.get("createdAt"))
        updated_at = as_dt(source.get("updatedAt"))
        user, _ = AppUser.objects.update_or_create(
            id=user_id,
            defaults={
                "privy_user_id": source.get("privyUserId"),
                "email": (source.get("email") or "").strip().lower(),
                "wallet_address": source.get("walletAddress"),
                "smart_wallet_address": source.get("smartWalletAddress"),
                "name": source.get("name") or fields.get("name", ""),
                "age": int(source.get("age") or fields.get("age") or 0),
                "occupation": source.get("occupation") or fields.get("occupation", ""),
                "gender": source.get("gender") or fields.get("gender", ""),
                "country": source.get("country") or fields.get("country") or "Korea",
                "residence": source.get("residence") or fields.get("locale", ""),
                "display_name": source.get("displayName") or fields.get("name", ""),
                "public_slug": source.get("publicSlug") or user_id,
                "avatar_url": source.get("avatarUrl"),
                "payout_address": source.get("payoutAddress"),
                "has_profile": bool(profile),
                "created_at": created_at,
                "updated_at": updated_at,
            },
        )
        if not profile:
            continue
        AppEducation.objects.filter(user=user).delete()
        for index, item in enumerate(fields.get("educations") or []):
            AppEducation.objects.create(
                id=scoped_child_id(user.id, item.get("id") or f"education-{index + 1}", f"education-{index + 1}"),
                user=user,
                education=item.get("education", ""),
                status=item.get("status") or "graduated",
                sort_order=index,
                created_at=as_dt(item.get("createdAt") or source.get("createdAt")),
                updated_at=as_dt(item.get("updatedAt") or source.get("updatedAt")),
            )
        AppCareer.objects.filter(user=user).delete()
        for index, item in enumerate(fields.get("careers") or []):
            AppCareer.objects.create(
                id=scoped_child_id(user.id, item.get("id") or f"career-{index + 1}", f"career-{index + 1}"),
                user=user,
                career=item.get("career", ""),
                start_date=item.get("startDate", ""),
                end_date=item.get("endDate", ""),
                status=item.get("status") or "employed",
                sort_order=index,
                created_at=as_dt(item.get("createdAt") or source.get("createdAt")),
                updated_at=as_dt(item.get("updatedAt") or source.get("updatedAt")),
            )
        create_search_document(AppSearchDocument, user, source)

    for source in store_values(StoreRecord, "data_fields"):
        user_id = source.get("userId") or source.get("profileId")
        if not user_id:
            continue
        user = AppUser.objects.filter(id=user_id).first()
        if not user:
            user = AppUser.objects.create(id=user_id, public_slug=user_id, display_name=user_id, name=user_id)
        field, _ = AppDataField.objects.update_or_create(
            id=source.get("id"),
            defaults={
                "user": user,
                "kind": source.get("kind") or "",
                "label": source.get("label") or source.get("kind") or "",
                "value_preview": source.get("valuePreview") or "",
                "access_mode": source.get("accessMode") or "free",
                "price_cents": int(source.get("priceCents") or 0),
                "currency": source.get("currency") or "IP",
                "requires_verification": bool(source.get("requiresVerification")),
                "verification_status": source.get("verificationStatus") or "not_required",
                "cdr_state": source.get("cdrState") or "off",
                "seller_address": source.get("sellerAddress"),
                "created_at": as_dt(source.get("createdAt")),
                "updated_at": as_dt(source.get("updatedAt")),
            },
        )
        if any(
            source.get(key)
            for key in [
                "cdrVaultUuid",
                "deployTxHash",
                "cdrLicenseIpId",
                "cdrLicenseTermsId",
                "ipaNftContract",
                "ipaTokenId",
                "licenseConfigTxHash",
            ]
        ):
            AppCdrVault.objects.update_or_create(
                field=field,
                defaults={
                    "id": f"{field.id}-cdr",
                    "network": "aeneid",
                    "cdr_vault_uuid": str(source.get("cdrVaultUuid") or "0"),
                    "owner_address": source.get("ipaRecipient") or source.get("sellerAddress") or user.wallet_address,
                    "allocate_tx_hash": source.get("cdrAllocateTxHash"),
                    "deploy_tx_hash": source.get("deployTxHash"),
                    "cdr_license_ip_id": source.get("cdrLicenseIpId"),
                    "cdr_license_terms_id": str(source.get("cdrLicenseTermsId")) if source.get("cdrLicenseTermsId") is not None else None,
                    "platform_wallet": source.get("platformWallet"),
                    "ipa_recipient": source.get("ipaRecipient"),
                    "ipa_nft_contract": source.get("ipaNftContract"),
                    "ipa_token_id": str(source.get("ipaTokenId")) if source.get("ipaTokenId") is not None else None,
                    "ip_registration_tx_hash": source.get("ipRegistrationTxHash"),
                    "ipa_transfer_tx_hash": source.get("ipaTransferTxHash"),
                    "license_config_tx_hash": source.get("licenseConfigTxHash"),
                    "license_attach_tx_hash": source.get("licenseAttachTxHash"),
                    "status": "active" if source.get("cdrState") == "on" else "allocating",
                    "created_at": as_dt(source.get("createdAt")),
                    "updated_at": as_dt(source.get("updatedAt")),
                },
            )

    for source in store_values(StoreRecord, "quotes"):
        AppQuote.objects.update_or_create(
            id=source.get("id"),
            defaults={
                "buyer_wallet": source.get("buyerWallet") or "",
                "prompt": source.get("prompt") or "",
                "filters": source.get("filters") or {},
                "recommended_fields": source.get("recommendedFields") or [],
                "wanted_fields": source.get("wantedFields") or [],
                "profile_ids": source.get("profileIds") or [],
                "matches": source.get("matches") or [],
                "extensions": source.get("extensions") or [],
                "matched_profile_count": int(source.get("matchedProfileCount") or 0),
                "paid_field_count": int(source.get("paidFieldCount") or 0),
                "free_field_count": int(source.get("freeFieldCount") or 0),
                "subtotal_cents": int(source.get("subtotalCents") or 0),
                "service_fee_cents": int(source.get("serviceFeeCents") or 0),
                "total_cents": int(source.get("totalCents") or 0),
                "currency": source.get("currency") or "IP",
                "batch_size": int(source.get("batchSize") or 100),
                "capped": bool(source.get("capped")),
                "max_paid_fields": int(source.get("maxPaidFields") or 800),
                "sheet_params": source.get("sheetParams") or {},
                "created_at": as_dt(source.get("createdAt")),
                "updated_at": as_dt(source.get("updatedAt") or source.get("createdAt")),
            },
        )

    for source in store_values(StoreRecord, "orders"):
        quote = AppQuote.objects.filter(id=source.get("quoteId")).first()
        order, _ = AppOrder.objects.update_or_create(
            id=source.get("id"),
            defaults={
                "quote": quote,
                "buyer_wallet": source.get("buyerWallet") or "",
                "prompt": source.get("prompt") or "",
                "filters": source.get("filters") or {},
                "selected_profile_ids": source.get("selectedProfileIds") or [],
                "selected_match_refs": source.get("selectedMatchRefs") or [],
                "selected_field_ids": source.get("selectedFieldIds") or [],
                "subtotal_cents": int(source.get("subtotalCents") or 0),
                "service_fee_cents": int(source.get("serviceFeeCents") or 0),
                "total_cents": int(source.get("totalCents") or 0),
                "currency": source.get("currency") or "IP",
                "batch_size": int(source.get("batchSize") or 100),
                "platform_fee_bps": int(source.get("platformFeeBps") or 0),
                "status": source.get("status") or "pending_payment",
                "payment_tx_hash": source.get("paymentTxHash"),
                "license_token_ids": source.get("licenseTokenIds") or [],
                "license_token_grants": source.get("licenseTokenGrants") or [],
                "purchase_contract": source.get("purchaseContract") or "",
                "access_proof": source.get("accessProof") or "",
                "sheet_params": source.get("sheetParams") or {},
                "created_at": as_dt(source.get("createdAt")),
                "updated_at": as_dt(source.get("updatedAt") or source.get("createdAt")),
            },
        )
        AppOrderItem.objects.filter(order=order).delete()
        grant_by_field = {grant.get("fieldId"): grant for grant in source.get("licenseTokenGrants") or []}
        match_by_profile = {
            profile_id: source.get("selectedMatchRefs", [])[index] if index < len(source.get("selectedMatchRefs", [])) else f"match-{index + 1}"
            for index, profile_id in enumerate(source.get("selectedProfileIds") or [])
        }
        for field_id in source.get("selectedFieldIds") or []:
            field = AppDataField.objects.filter(id=field_id).select_related("user").first()
            if not field:
                continue
            grant = grant_by_field.get(field_id) or {}
            AppOrderItem.objects.create(
                order=order,
                field=field,
                seller_user=field.user,
                match_ref=match_by_profile.get(field.user_id, ""),
                kind=field.kind,
                price_cents=field.price_cents,
                seller_address=field.seller_address or field.user.wallet_address or "",
                cdr_vault_uuid=getattr(getattr(field, "cdr_vault", None), "cdr_vault_uuid", None),
                license_token_id=str(grant.get("licenseTokenId")) if grant.get("licenseTokenId") is not None else None,
                license_mint_tx_hash=grant.get("mintTxHash"),
                created_at=as_dt(source.get("createdAt")),
            )
        AppOrderSellerPayout.objects.filter(order=order).delete()
        for payout in source.get("sellerPayouts") or []:
            AppOrderSellerPayout.objects.create(
                order=order,
                seller_address=payout.get("sellerAddress") or "",
                field_ids=payout.get("fieldIds") or [],
                gross_cents=int(payout.get("grossCents") or 0),
                seller_cents=int(payout.get("sellerCents") or 0),
                service_fee_cents=int(payout.get("serviceFeeCents") or 0),
                created_at=as_dt(source.get("createdAt")),
            )

    for source in store_values(StoreRecord, "onchain_sales"):
        field = AppDataField.objects.filter(id=source.get("fieldId")).first()
        order = AppOrder.objects.filter(id=source.get("orderId")).first()
        AppOnchainSale.objects.update_or_create(
            id=source.get("id"),
            defaults={
                "order": order,
                "field": field,
                "buyer_wallet": source.get("buyerWallet") or "",
                "seller_address": source.get("sellerAddress") or "",
                "kind": source.get("kind") or "",
                "label": source.get("label") or "Paid data",
                "cdr_license_ip_id": source.get("cdrLicenseIpId"),
                "gross_cents": int(source.get("grossCents") or 0),
                "seller_cents": int(source.get("sellerCents") or 0),
                "service_fee_cents": int(source.get("serviceFeeCents") or 0),
                "payment_tx_hash": source.get("paymentTxHash") or "",
                "block_number": str(source.get("blockNumber")) if source.get("blockNumber") is not None else None,
                "log_index": source.get("logIndex"),
                "source": source.get("source") or "onchain",
                "created_at": as_dt(source.get("createdAt")),
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0002_rename_store_namespace_index"),
        ("users", "0001_initial"),
        ("data", "0001_initial"),
        ("onchain", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(migrate_store_records, migrations.RunPython.noop),
    ]

