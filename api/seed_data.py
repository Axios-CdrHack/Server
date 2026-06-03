from .constants import DEFAULT_WANTED_FIELDS

NOW = "2026-05-21T00:00:00.000Z"

PROFILE_INPUTS = [
    {
        "slug": "jaine-k7q3p9",
        "displayName": "Jaine",
        "wallet": "0x1111111111111111111111111111111111111111",
        "email": "jane.pm@example.com",
        "publicFields": {
            "name": "Jaine",
            "gender": "female",
            "age": 30,
            "country": "Korea",
            "locale": "Seoul",
            "occupation": "IT Product Manager",
            "education": "Yonsei University",
            "educationStatus": "graduated",
            "educations": [{"id": "education-1", "education": "Yonsei University", "status": "graduated"}],
            "career": "8 years in product",
            "careerWorkYears": 8,
            "careerStartDate": "2018-03",
            "careerEndDate": "2026-02",
            "careerStatus": "left",
            "careers": [{"id": "career-1", "career": "Product Manager", "startDate": "2018-03", "endDate": "2026-02", "status": "left"}],
        },
        "contacts": {
            "email": "jane.pm@example.com",
            "mobile": "+82 10 0101 0101",
            "telegram": "@jainepm",
            "discord": "jaine.pm",
            "twitter": "@jainepm",
        },
    },
    {
        "slug": "platform-operator-9d2hvk",
        "displayName": "Platform Operator",
        "wallet": "0x2222222222222222222222222222222222222222",
        "email": "ops.tpm@example.com",
        "publicFields": {
            "name": "Platform Operator",
            "gender": "nonbinary",
            "age": 31,
            "country": "Singapore",
            "locale": "Singapore",
            "occupation": "Technical Program Manager",
            "education": "National University of Singapore",
            "educationStatus": "graduated",
            "educations": [{"id": "education-1", "education": "National University of Singapore", "status": "graduated"}],
            "career": "7 years in platform operations",
            "careerWorkYears": 7,
            "careerStartDate": "2019-01",
            "careerEndDate": "2025-12",
            "careerStatus": "left",
            "careers": [{"id": "career-1", "career": "Technical Program Manager", "startDate": "2019-01", "endDate": "2025-12", "status": "left"}],
        },
        "contacts": {
            "email": "ops.tpm@example.com",
            "mobile": "+65 9000 1111",
            "telegram": "@ops_tpm",
            "discord": "ops.tpm",
            "twitter": "@ops_tpm",
        },
    },
    {
        "slug": "fintech-maker-v3r8n2",
        "displayName": "Fintech Maker",
        "wallet": "0x3333333333333333333333333333333333333333",
        "email": "fintech.pm@example.com",
        "publicFields": {
            "name": "Fintech Maker",
            "gender": "male",
            "age": 29,
            "country": "United States",
            "locale": "New York",
            "occupation": "Product Manager",
            "education": "New York University",
            "educationStatus": "graduated",
            "educations": [{"id": "education-1", "education": "New York University", "status": "graduated"}],
            "career": "6 years in fintech product",
            "careerWorkYears": 6,
            "careerStartDate": "2020-04",
            "careerEndDate": "2026-03",
            "careerStatus": "left",
            "careers": [{"id": "career-1", "career": "Product Manager", "startDate": "2020-04", "endDate": "2026-03", "status": "left"}],
        },
        "contacts": {
            "email": "fintech.pm@example.com",
            "mobile": "+1 212 555 0141",
            "telegram": "@fintechmaker",
            "discord": "fintech.pm",
            "twitter": "@fintechmaker",
        },
    },
    {
        "slug": "growth-pm-h8t4kc",
        "displayName": "Growth PM",
        "wallet": "0x5555555555555555555555555555555555555555",
        "email": "growth.pm@example.com",
        "publicFields": {
            "name": "Growth PM",
            "gender": "female",
            "age": 32,
            "country": "Germany",
            "locale": "Berlin",
            "occupation": "Growth Product Lead",
            "education": "Humboldt University of Berlin",
            "educationStatus": "graduated",
            "educations": [{"id": "education-1", "education": "Humboldt University of Berlin", "status": "graduated"}],
            "career": "9 years in growth strategy",
            "careerWorkYears": 9,
            "careerStartDate": "2017-06",
            "careerEndDate": "2026-04",
            "careerStatus": "left",
            "careers": [{"id": "career-1", "career": "Growth Product Lead", "startDate": "2017-06", "endDate": "2026-04", "status": "left"}],
        },
        "contacts": {
            "email": "growth.pm@example.com",
            "mobile": "+49 30 5555 0188",
            "telegram": "@growthpm",
            "discord": "growth.pm",
            "twitter": "@growthpm",
        },
    },
    {
        "slug": "sales-lead-z2n6qa",
        "displayName": "Enterprise Seller",
        "wallet": "0x4444444444444444444444444444444444444444",
        "email": "sales.manager@example.com",
        "publicFields": {
            "name": "Enterprise Seller",
            "gender": "male",
            "age": 34,
            "country": "United States",
            "locale": "San Francisco",
            "occupation": "Sales Manager",
            "education": "University of California",
            "educationStatus": "graduated",
            "educations": [{"id": "education-1", "education": "University of California", "status": "graduated"}],
            "career": "10 years in enterprise sales",
            "careerWorkYears": 10,
            "careerStartDate": "2016-02",
            "careerEndDate": "2026-01",
            "careerStatus": "left",
            "careers": [{"id": "career-1", "career": "Sales Manager", "startDate": "2016-02", "endDate": "2026-01", "status": "left"}],
        },
        "contacts": {
            "email": "sales.manager@example.com",
            "mobile": "+1 415 555 0177",
            "telegram": "@enterprise_sales",
            "discord": "enterprise.sales",
            "twitter": "@enterprise_sales",
        },
    },
]


def masked(kind, value):
    if kind == "email":
        return value
    if kind == "mobile":
        prefix = value[:-4]
        suffix = value[-4:]
        return "".join("*" if char.isdigit() else char for char in prefix) + suffix
    if value.startswith("@"):
        return f"{value[:3]}***"
    return f"{value[:10]}..."


def build_seed():
    profiles = []
    users = []
    fields = []

    for index, item in enumerate(PROFILE_INPUTS):
        profile_id = f"user-{index + 1}"
        public_fields = dict(item["publicFields"])
        profile = {
            "id": profile_id,
            "email": item["email"],
            "publicSlug": item["slug"],
            "displayName": item["displayName"],
            "walletAddress": item["wallet"],
            "payoutAddress": item["wallet"],
            "name": public_fields["name"],
            "age": public_fields["age"],
            "occupation": public_fields["occupation"],
            "gender": public_fields["gender"],
            "country": public_fields["country"],
            "residence": public_fields["locale"],
            "educations": public_fields["educations"],
            "careers": public_fields["careers"],
            "publicFields": public_fields,
            "createdAt": NOW,
            "updatedAt": NOW,
        }
        profiles.append(profile)
        users.append(
            {
                "id": profile_id,
                "email": item["email"],
                "walletAddress": item["wallet"],
                "name": public_fields["name"],
                "age": public_fields["age"],
                "occupation": public_fields["occupation"],
                "gender": public_fields["gender"],
                "country": public_fields["country"],
                "residence": public_fields["locale"],
                "displayName": item["displayName"],
                "publicSlug": item["slug"],
                "payoutAddress": item["wallet"],
                "createdAt": NOW,
                "updatedAt": NOW,
            }
        )

        prices = {"email": 900, "mobile": 1400, "telegram": 450, "discord": 350, "twitter": 300}
        for kind in DEFAULT_WANTED_FIELDS:
            value = item["contacts"][kind]
            requires_verification = kind == "mobile"
            fields.append(
                {
                    "id": f"{profile_id}-{kind}",
                    "userId": profile_id,
                    "profileId": profile_id,
                    "kind": kind,
                    "label": "Mobile" if kind == "mobile" else kind[:1].upper() + kind[1:],
                    "valuePreview": masked(kind, value),
                    "accessMode": "paid",
                    "priceCents": prices[kind],
                    "currency": "IP",
                    "requiresVerification": requires_verification,
                    "verificationStatus": "verified" if requires_verification else "not_required",
                    "cdrState": "off",
                    "sellerAddress": item["wallet"],
                    "createdAt": NOW,
                    "updatedAt": NOW,
                }
            )

    return users, profiles, fields
