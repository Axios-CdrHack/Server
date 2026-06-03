import json
import os

import requests

from . import repository
from main.constants import BATCH_SIZE, DEFAULT_WANTED_FIELDS, MAX_PAID_FIELDS_PER_ORDER, PLATFORM_FEE_BPS
from main.errors import ProviderNotConfiguredError

GEMINI_MODEL = "gemini-2.5-flash"


def gemini_api_url():
    base_url = os.environ.get("GEMINI_API_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise ProviderNotConfiguredError(message="gemini_api_base_url_not_configured")
    if base_url.endswith(":generateContent"):
        return base_url
    if "/models/" in base_url:
        return f"{base_url}:generateContent"
    if not base_url.endswith(("/v1", "/v1beta")):
        base_url = f"{base_url}/v1beta"
    return f"{base_url}/models/{GEMINI_MODEL}:generateContent"


class GeminiIntentError(Exception):
    pass


def unique_normalized(values):
    return list(dict.fromkeys([value.strip() for value in values if isinstance(value, str) and value.strip()]))


def vocabulary():
    documents = repository.get_search_documents()
    return {
        "countries": unique_normalized([doc["country"] for doc in documents]),
        "locales": unique_normalized([doc["locale"] for doc in documents]),
        "occupations": unique_normalized([doc["occupation"] for doc in documents]),
        "genders": unique_normalized([doc["gender"] for doc in documents]),
    }


def match_allowed_value(value, allowed, aliases=None):
    if not value:
        return None
    aliases = aliases or {}
    normalized = value.strip().lower()
    if normalized in aliases:
        return aliases[normalized]
    exact = next((item for item in allowed if item.lower() == normalized), None)
    if exact:
        return exact
    return next((item for item in allowed if normalized in item.lower() or item.lower() in normalized), None)


def normalize_terms(terms):
    return list(dict.fromkeys([term.strip().lower() for term in terms if isinstance(term, str) and term.strip()]))


def normalize_occupation(value, allowed):
    exact = match_allowed_value(value, allowed)
    if exact:
        return exact
    if not value:
        return None
    normalized = value.strip().lower()
    if "sales" in normalized:
        return "Sales Manager"
    if "growth" in normalized:
        return "Growth Product Lead"
    if "technical program" in normalized:
        return "Technical Program Manager"
    if "product" in normalized or "pm" in normalized:
        return "Product Manager"
    return normalized


def build_prompt(prompt):
    vocab = vocabulary()
    return "\n".join(
        [
            "You are the AXIOS search intent parser for an anonymous paid data marketplace.",
            "Return JSON only. Do not explain anything.",
            "",
            "Public searchable fields: age range, gender, country, locale, occupation.",
            f"Allowed country values: {', '.join(vocab['countries'])}",
            f"Allowed locale values: {', '.join(vocab['locales'])}",
            f"Observed occupation values: {', '.join(vocab['occupations'])}",
            f"Allowed gender values: {', '.join(vocab['genders'])}",
            f"Paid parameter options: {', '.join(DEFAULT_WANTED_FIELDS)}",
            "",
            "JSON keys: minAge, maxAge, gender, country, locale, occupation, terms, recommendedFields.",
            f"User prompt: {prompt}",
        ]
    )


def normalize_filters(parsed):
    vocab = vocabulary()
    return {
        "minAge": parsed.get("minAge") or None,
        "maxAge": parsed.get("maxAge") or None,
        "gender": match_allowed_value(
            parsed.get("gender"),
            vocab["genders"],
            {"woman": "female", "women": "female", "female": "female", "man": "male", "men": "male", "male": "male"},
        ),
        "country": match_allowed_value(
            parsed.get("country"),
            vocab["countries"],
            {"south korea": "Korea", "korea": "Korea", "usa": "United States", "us": "United States", "america": "United States"},
        ),
        "locale": match_allowed_value(
            parsed.get("locale"),
            vocab["locales"],
            {"seoul": "Seoul", "nyc": "New York", "san fran": "San Francisco", "sf": "San Francisco"},
        ),
        "occupation": normalize_occupation(parsed.get("occupation"), vocab["occupations"]),
        "terms": normalize_terms(parsed.get("terms") or [str(parsed.get("occupation") or "")]),
    }


def analyze_search_intent(prompt):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ProviderNotConfiguredError(message="gemini_provider_not_configured")
    response = requests.post(
        gemini_api_url(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json={
            "contents": [{"parts": [{"text": build_prompt(prompt)}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        },
        timeout=12,
    )
    if response.status_code >= 400:
        raise GeminiIntentError(f"gemini_intent_http_{response.status_code}")
    payload = response.json()
    text = "".join(part.get("text", "") for part in payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])).strip()
    if not text:
        raise GeminiIntentError("gemini_intent_empty_response")
    parsed = json.loads(text)
    fields = [field for field in parsed.get("recommendedFields", DEFAULT_WANTED_FIELDS) if field in DEFAULT_WANTED_FIELDS]
    return {"filters": normalize_filters(parsed), "recommendedFields": fields or list(DEFAULT_WANTED_FIELDS)}


def matches_public_field(value, target):
    if not target:
        return True
    return target.lower() in (value or "").lower()


def matches_occupation(document, filters):
    target = filters.get("occupation")
    if not target:
        return True
    haystack = f"{document['occupation']} {' '.join(document['tags'])}".lower()
    normalized = target.lower()
    if normalized in haystack:
        return True
    terms = [term for term in normalized.split() if len(term) >= 2]
    return any(term in haystack for term in terms) if terms else True


def score_document(document, filters):
    return sum(1 for term in filters.get("terms", []) if term in document["text"])


def rank_profile_ids(filters, limit=BATCH_SIZE):
    documents = [
        doc
        for doc in repository.get_search_documents()
        if (not filters.get("minAge") or doc["age"] >= filters["minAge"])
        and (not filters.get("maxAge") or doc["age"] <= filters["maxAge"])
        and matches_occupation(doc, filters)
        and matches_public_field(doc["country"], filters.get("country"))
        and matches_public_field(doc["locale"], filters.get("locale"))
        and matches_public_field(doc["gender"], filters.get("gender"))
    ]
    return [item["profileId"] for item in sorted(documents, key=lambda doc: score_document(doc, filters), reverse=True)[:limit]]


def is_chargeable(field):
    return field.get("accessMode") == "paid" and field.get("cdrState") == "on"


def is_available(field):
    return (
        is_chargeable(field)
        and repository.field_has_mintable_license(field)
        and (not field.get("requiresVerification") or field.get("verificationStatus") == "verified")
    )


def requested_fields(fields, wanted_fields):
    wanted = set(wanted_fields)
    return [field for field in fields if field.get("kind") in wanted and is_available(field)]


def filter_profiles_with_requested_cdr_fields(profile_ids, wanted_fields):
    eligible = {field["userId"] for field in requested_fields(repository.get_fields_by_profile_ids(profile_ids), wanted_fields)}
    return [profile_id for profile_id in profile_ids if profile_id in eligible]


def document_signals(document, filters):
    signals = []
    for key, label in [("minAge", "age fit"), ("occupation", "occupation fit"), ("country", "country fit"), ("locale", "locale fit"), ("gender", "gender fit")]:
        if filters.get(key):
            signals.append(label)
    signals.extend([document["country"], document["locale"], document["occupation"]])
    return list(dict.fromkeys([item for item in signals if item]))[:5]


def build_matches(profile_ids, filters, wanted_fields):
    docs = {doc["profileId"]: doc for doc in repository.get_search_documents()}
    fields_by_profile = {}
    for field in repository.get_fields_by_profile_ids(profile_ids):
        fields_by_profile.setdefault(field["userId"], []).append(field)

    matches = []
    for index, profile_id in enumerate(profile_ids):
        profile_fields = requested_fields(fields_by_profile.get(profile_id, []), wanted_fields)
        paid = [field for field in profile_fields if is_chargeable(field)]
        free = [field for field in profile_fields if not is_chargeable(field)]
        matches.append(
            {
                "matchRef": f"match-{index + 1}",
                "signals": document_signals(docs[profile_id], filters) if profile_id in docs else [],
                "fieldCosts": [
                    {
                        "fieldId": field["id"],
                        "kind": field["kind"],
                        "label": field["label"],
                        "priceCents": field["priceCents"] if is_chargeable(field) else 0,
                        "accessMode": field["accessMode"],
                        "cdrState": field["cdrState"],
                        "cdrLicenseIpId": field["cdrLicenseIpId"],
                        "cdrLicenseTermsId": field["cdrLicenseTermsId"],
                        "ipaNftContract": field.get("ipaNftContract"),
                        "ipaTokenId": field.get("ipaTokenId"),
                        "ipRegistrationTxHash": field.get("ipRegistrationTxHash"),
                        "licenseConfigTxHash": field.get("licenseConfigTxHash"),
                    }
                    for field in profile_fields
                ],
                "subtotalCents": sum(field["priceCents"] for field in paid),
                "paidFieldCount": len(paid),
                "freeFieldCount": len(free),
            }
        )
    return matches


def find_matching_profile_ids(prompt, limit=BATCH_SIZE, wanted_fields=None):
    intent = analyze_search_intent(prompt)
    requested = wanted_fields or intent["recommendedFields"]
    return {
        "filters": intent["filters"],
        "recommendedFields": intent["recommendedFields"],
        "profileIds": filter_profiles_with_requested_cdr_fields(rank_profile_ids(intent["filters"], limit), requested),
    }


def quote_totals(profile_ids, wanted_fields):
    fields = requested_fields(repository.get_fields_by_profile_ids(profile_ids), wanted_fields)
    paid = [field for field in fields if is_chargeable(field)]
    free = [field for field in fields if not is_chargeable(field)]
    capped_paid = paid[:MAX_PAID_FIELDS_PER_ORDER]
    subtotal = sum(field["priceCents"] for field in capped_paid)
    service_fee = round(subtotal * PLATFORM_FEE_BPS / 10000)
    return {
        "matchedProfileCount": len(profile_ids),
        "paidFieldCount": len(capped_paid),
        "freeFieldCount": len(free),
        "subtotalCents": subtotal,
        "serviceFeeCents": service_fee,
        "totalCents": subtotal + service_fee,
        "currency": "IP",
        "batchSize": BATCH_SIZE,
        "capped": len(capped_paid) < len(paid),
        "maxPaidFields": MAX_PAID_FIELDS_PER_ORDER,
    }


def build_quote_detail(quote):
    wanted_fields = quote.get("wantedFields") or quote.get("recommendedFields") or list(DEFAULT_WANTED_FIELDS)
    filters = quote.get("filters") or {}
    profile_ids = quote.get("profileIds") or []
    totals = quote_totals(profile_ids, wanted_fields)
    matches = build_matches(profile_ids, filters, wanted_fields)
    timestamp = quote.get("createdAt") or repository.now_iso()
    return {
        "id": quote["id"],
        "buyerWallet": quote.get("buyerWallet", ""),
        "prompt": quote.get("prompt", ""),
        "filters": filters,
        "recommendedFields": quote.get("recommendedFields") or wanted_fields,
        "wantedFields": wanted_fields,
        "profileIds": profile_ids,
        "matches": matches,
        "extensions": quote.get("extensions", []),
        **totals,
        "prePurchaseNotice": "Anonymous quote only. Identity and paid contact values stay hidden until batch access is purchased.",
        "sheetParams": quote.get("sheetParams")
        or {
            "prompt": quote.get("prompt", ""),
            "filters": filters,
            "fields": wanted_fields,
            "sort": "relevance",
            "generatedAt": timestamp,
        },
        "createdAt": timestamp,
    }


def extend_quote(quote, prompt):
    wanted_fields = quote.get("wantedFields") or quote.get("recommendedFields") or list(DEFAULT_WANTED_FIELDS)
    resolved = find_matching_profile_ids(prompt, BATCH_SIZE, wanted_fields)
    existing_profile_ids = quote.get("profileIds") or []
    existing = set(existing_profile_ids)
    added_profile_ids = [profile_id for profile_id in resolved["profileIds"] if profile_id not in existing]
    next_profile_ids = [*existing_profile_ids, *added_profile_ids]
    timestamp = repository.now_iso()
    totals = quote_totals(next_profile_ids, wanted_fields)
    next_quote = {
        **quote,
        "wantedFields": wanted_fields,
        "profileIds": next_profile_ids,
        "extensions": [
            *(quote.get("extensions") or []),
            {
                "prompt": prompt,
                "filters": resolved["filters"],
                "addedProfileIds": added_profile_ids,
                "createdAt": timestamp,
            },
        ],
        "updatedAt": timestamp,
        **totals,
    }
    next_quote["matches"] = build_matches(next_profile_ids, next_quote.get("filters") or {}, wanted_fields)
    repository.save_quote(next_quote)
    return build_quote_detail(next_quote)


def build_quote(request_body):
    intent = analyze_search_intent(request_body["prompt"])
    wanted_fields = request_body.get("wantedFields") or intent["recommendedFields"]
    profile_ids = filter_profiles_with_requested_cdr_fields(rank_profile_ids(intent["filters"], BATCH_SIZE), wanted_fields)
    matches = build_matches(profile_ids, intent["filters"], wanted_fields)
    totals = quote_totals(profile_ids, wanted_fields)
    quote_id = f"quote-{repository.nanoid(10)}"
    timestamp = repository.now_iso()
    repository.save_quote(
        {
            "id": quote_id,
            "buyerWallet": request_body.get("buyerWallet", ""),
            "prompt": request_body["prompt"],
            "filters": intent["filters"],
            "recommendedFields": intent["recommendedFields"],
            "wantedFields": wanted_fields,
            "profileIds": profile_ids,
            "matches": matches,
            "extensions": [],
            **totals,
            "sheetParams": {
                "prompt": request_body["prompt"],
                "filters": intent["filters"],
                "fields": wanted_fields,
                "sort": "relevance",
                "generatedAt": timestamp,
            },
            "createdAt": timestamp,
        }
    )
    return {
        "quoteId": quote_id,
        "prompt": request_body["prompt"],
        "filters": intent["filters"],
        "recommendedFields": wanted_fields,
        "matches": matches,
        **totals,
        "prePurchaseNotice": "Anonymous quote only. Identity and paid contact values stay hidden until batch access is purchased.",
        "sheetParams": {
            "prompt": request_body["prompt"],
            "filters": intent["filters"],
            "fields": wanted_fields,
            "sort": "relevance",
            "generatedAt": timestamp,
        },
    }
