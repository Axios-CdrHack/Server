from eth_abi import encode
from web3 import Web3

from . import repository
from .constants import BATCH_SIZE, MAX_PAID_FIELDS_PER_ORDER, PLATFORM_FEE_BPS, PURCHASE_CONTRACT_ADDRESS, SELLER_SHARE_BPS
from .errors import ApiError
from .license_verification import verify_license_tokens_owned_by
from .search import find_matching_profile_ids

TX_HASH_RE = r"^0x[a-fA-F0-9]{64}$"


def normalize_license_token_ids(token_ids):
    result = []
    for token_id in token_ids or []:
        if not str(token_id).isdigit():
            raise ApiError("invalid_license_token_id", status_code=400)
        result.append(int(token_id))
    return result


def build_access_aux_data(order):
    return "0x" + encode(["uint256[]"], [normalize_license_token_ids(order.get("licenseTokenIds", []))]).hex()


def is_chargeable(field):
    return field.get("accessMode") == "paid" and field.get("cdrState") == "on"


def is_purchasable(field):
    return (
        is_chargeable(field)
        and bool(field.get("sellerAddress"))
        and repository.field_has_mintable_license(field)
        and (not field.get("requiresVerification") or field.get("verificationStatus") == "verified")
    )


def build_seller_payouts(field_ids):
    payouts = {}
    for field in [field for field in repository.get_fields_by_ids(field_ids) if is_purchasable(field)]:
        seller = field.get("sellerAddress")
        if not seller:
            continue
        service_fee = round(field["priceCents"] * PLATFORM_FEE_BPS / 10000)
        seller_cents = round(field["priceCents"] * SELLER_SHARE_BPS / 10000)
        payout = payouts.setdefault(seller, {"sellerAddress": seller, "fieldIds": [], "grossCents": 0, "sellerCents": 0, "serviceFeeCents": 0})
        payout["fieldIds"].append(field["id"])
        payout["grossCents"] += field["priceCents"]
        payout["sellerCents"] += seller_cents
        payout["serviceFeeCents"] += service_fee
    return list(payouts.values())


def validate_license_grant(grant):
    if not str(grant.get("licenseTokenId", "")).isdigit():
        raise ApiError("invalid_license_token_id", status_code=400)
    import re

    if not re.fullmatch(TX_HASH_RE, str(grant.get("mintTxHash", ""))):
        raise ApiError("invalid_license_mint_tx_hash", status_code=400)


def create_order(input_data, owner_wallets=None):
    order_id = f"order-{repository.nanoid(10)}"
    stored_quote = repository.get_quote(input_data.get("quoteId")) if input_data.get("quoteId") else None
    if stored_quote and stored_quote.get("prompt") == input_data["prompt"]:
        filters = stored_quote["filters"]
        profile_ids = stored_quote["profileIds"]
        quote_id = stored_quote["id"]
    else:
        resolved = find_matching_profile_ids(input_data["prompt"], BATCH_SIZE, input_data.get("wantedFields"))
        filters = resolved["filters"]
        profile_ids = resolved["profileIds"]
        quote_id = input_data.get("quoteId") or f"quote-{repository.nanoid(10)}"

    selected_refs = set(input_data.get("selectedMatchRefs") or [])
    candidates = [
        {"profileId": profile_id, "matchRef": f"match-{index + 1}"}
        for index, profile_id in enumerate(profile_ids)
        if not selected_refs or f"match-{index + 1}" in selected_refs
    ]
    wanted = set(input_data["wantedFields"])
    fields = [
        field
        for field in repository.get_fields_by_profile_ids([item["profileId"] for item in candidates])
        if field["kind"] in wanted and is_purchasable(field)
    ][:MAX_PAID_FIELDS_PER_ORDER]
    selected_field_ids = [field["id"] for field in fields]
    if not selected_field_ids:
        raise ApiError("no_purchasable_fields", status_code=500)

    grants = input_data.get("licenseTokenGrants") or []
    for grant in grants:
        validate_license_grant(grant)
    grant_by_field = {grant["fieldId"]: grant for grant in grants}
    token_ids = {grant["licenseTokenId"] for grant in grants}
    if len(grants) != len(selected_field_ids) or len(grant_by_field) != len(grants) or len(token_ids) != len(grants) or not all(field_id in grant_by_field for field_id in selected_field_ids):
        raise ApiError("license_token_grants_incomplete", status_code=500)

    ordered_grants = [grant_by_field[field_id] for field_id in selected_field_ids]
    license_token_ids = [grant["licenseTokenId"] for grant in ordered_grants]
    verify_license_tokens_owned_by(owner_wallets or [input_data["buyerWallet"]], license_token_ids)

    eligible_profiles = {field["userId"] for field in fields}
    selected_profiles = [item for item in candidates if item["profileId"] in eligible_profiles]
    subtotal = sum(field["priceCents"] for field in fields)
    service_fee = round(subtotal * PLATFORM_FEE_BPS / 10000)
    timestamp = repository.now_iso()
    order = {
        "id": order_id,
        "quoteId": quote_id,
        "buyerWallet": input_data["buyerWallet"],
        "prompt": input_data["prompt"],
        "filters": filters,
        "selectedProfileIds": [item["profileId"] for item in selected_profiles],
        "selectedMatchRefs": [item["matchRef"] for item in selected_profiles],
        "selectedFieldIds": selected_field_ids,
        "subtotalCents": subtotal,
        "serviceFeeCents": service_fee,
        "totalCents": subtotal + service_fee,
        "currency": "IP",
        "batchSize": BATCH_SIZE,
        "platformFeeBps": PLATFORM_FEE_BPS,
        "status": "paid",
        "paymentTxHash": input_data.get("paymentTxHash") or ordered_grants[0].get("mintTxHash"),
        "licenseTokenIds": license_token_ids,
        "licenseTokenGrants": ordered_grants,
        "purchaseContract": PURCHASE_CONTRACT_ADDRESS,
        "accessProof": Web3.keccak(text=f"{order_id}:{input_data['buyerWallet']}:{','.join(selected_field_ids)}").hex(),
        "sheetParams": {
            "orderId": order_id,
            "prompt": input_data["prompt"],
            "filters": filters,
            "fields": input_data["wantedFields"],
            "sort": "relevance",
            "generatedAt": timestamp,
        },
        "sellerPayouts": build_seller_payouts(selected_field_ids),
        "createdAt": timestamp,
        "updatedAt": timestamp,
    }
    return repository.save_order(order)


def summarize_order(order):
    return {
        key: order.get(key)
        for key in [
            "id",
            "quoteId",
            "buyerWallet",
            "prompt",
            "selectedMatchRefs",
            "selectedFieldIds",
            "subtotalCents",
            "serviceFeeCents",
            "totalCents",
            "currency",
            "status",
            "paymentTxHash",
            "licenseTokenIds",
            "licenseTokenGrants",
            "purchaseContract",
            "sheetParams",
            "createdAt",
            "updatedAt",
        ]
    }


def get_export_plan(order_id):
    order = repository.get_order(order_id)
    if not order:
        return None
    fields = repository.get_fields_by_ids(order.get("selectedFieldIds", []))
    grant_by_field = {grant["fieldId"]: grant for grant in order.get("licenseTokenGrants", [])}
    match_by_profile = {
        profile_id: order.get("selectedMatchRefs", [])[index] if index < len(order.get("selectedMatchRefs", [])) else f"match-{index + 1}"
        for index, profile_id in enumerate(order.get("selectedProfileIds", []))
    }
    return {
        "orderId": order["id"],
        "buyerWallet": order["buyerWallet"],
        "columns": order["sheetParams"]["fields"],
        "sheetParams": order["sheetParams"],
        "items": [
            {
                "profileRef": match_by_profile.get(field["userId"], "match-unknown"),
                "fieldId": field["id"],
                "kind": field["kind"],
                "label": field["label"],
                "accessMode": field["accessMode"],
                "cdrState": field["cdrState"],
                "cdrVaultUuid": field.get("cdrVaultUuid"),
                "priceCents": field["priceCents"],
                "licenseTokenIds": [grant_by_field[field["id"]]["licenseTokenId"]] if field["id"] in grant_by_field else [],
                "accessAuxData": build_access_aux_data({"licenseTokenIds": [grant_by_field[field["id"]]["licenseTokenId"]] if field["id"] in grant_by_field else []}),
            }
            for field in fields
        ],
    }
