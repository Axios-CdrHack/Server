import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from data import repository
from data.integrations import deploy_field_cdr_with_server_wallet, upload_field_ip_metadata


DEFAULT_RECIPIENT_WALLET = "0xE34CD8C9C1561A575a24bE5A5E0e883D96E28f81"
DEFAULT_USER_COUNT = 100
DEFAULT_KINDS = ("email", "mobile", "telegram", "discord", "twitter", "insurance", "height", "weight", "blood_type")
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

FIELD_CONFIGS = {
    "email": {"label": "Email", "price": 900},
    "mobile": {"label": "Mobile", "price": 1400},
    "telegram": {"label": "Telegram", "price": 450},
    "discord": {"label": "Discord", "price": 350},
    "twitter": {"label": "Twitter", "price": 300},
    "insurance": {"label": "Insurance Data", "price": 2200},
    "height": {"label": "Height", "price": 250},
    "weight": {"label": "Weight", "price": 250},
    "blood_type": {"label": "Blood Type", "price": 300},
}

BLOOD_TYPES = ("A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-")
INSURANCE_PLANS = (
    "National Health Insurance + private indemnity",
    "Employer PPO + dental rider",
    "Public healthcare + cancer rider",
    "Private HMO + annual wellness",
    "Travel medical + outpatient rider",
    "High deductible plan + HSA",
    "International expat medical",
    "Family health plan + vision",
)

PERSONAS = [
    ("Product Ops Lead", "Product Manager", "consumer product launch"),
    ("Growth Analyst", "Growth Product Lead", "fintech growth"),
    ("Technical Program Lead", "Technical Program Manager", "cloud platform delivery"),
    ("Enterprise Sales Lead", "Enterprise Sales Manager", "B2B enterprise sales"),
    ("Data Partnerships PM", "Data Partnerships Manager", "data marketplace partnerships"),
    ("Marketplace Researcher", "Market Research Manager", "survey operations"),
    ("AI Platform PM", "AI Platform Product Manager", "AI workflow tooling"),
    ("Payments TPM", "Payments Program Manager", "payments infrastructure"),
    ("Creator Growth Lead", "Creator Growth Manager", "creator monetization"),
    ("Enterprise Account Lead", "Enterprise Account Executive", "enterprise account strategy"),
    ("Healthcare Product Lead", "Healthcare Product Manager", "digital health onboarding"),
    ("Retail Insights Manager", "Market Research Manager", "omnichannel retail analytics"),
    ("Security Program Lead", "Technical Program Manager", "identity and access programs"),
    ("Climate Data PM", "Climate Data Product Lead", "climate data products"),
    ("Gaming Community Lead", "Gaming Community Manager", "game community growth"),
    ("Logistics Ops Strategist", "Logistics Operations Manager", "last-mile logistics"),
    ("Education Platform PM", "Education Product Manager", "online learning platforms"),
    ("Media Partnerships Lead", "Partnerships Manager", "media licensing partnerships"),
    ("Travel Marketplace Lead", "Travel Marketplace Manager", "travel marketplace expansion"),
    ("Crypto Research Lead", "Market Research Manager", "web3 user research"),
    ("Banking API TPM", "Banking API Program Manager", "open banking APIs"),
    ("SMB Revenue Lead", "SMB Revenue Manager", "SMB revenue operations"),
    ("HR Tech PM", "HR Tech Product Manager", "talent platform workflows"),
    ("Insurance Data Lead", "Insurance Data Product Manager", "insurance underwriting data"),
    ("Food Delivery Analyst", "Food Delivery Operations Analyst", "delivery marketplace efficiency"),
]

LOCATIONS = [
    ("Korea", "Seoul"),
    ("United States", "New York"),
    ("Singapore", "Singapore"),
    ("United States", "San Francisco"),
    ("Germany", "Berlin"),
    ("Korea", "Busan"),
    ("Japan", "Tokyo"),
    ("United Kingdom", "London"),
    ("Canada", "Toronto"),
    ("France", "Paris"),
    ("Australia", "Sydney"),
    ("India", "Bangalore"),
    ("Brazil", "Sao Paulo"),
    ("Netherlands", "Amsterdam"),
    ("United Arab Emirates", "Dubai"),
    ("Spain", "Madrid"),
    ("Mexico", "Mexico City"),
]

EDUCATIONS = [
    "Yonsei University",
    "NYU Stern",
    "National University of Singapore",
    "UC Berkeley",
    "Technical University of Munich",
    "Korea University",
    "Stanford University",
    "INSEAD",
    "Humboldt University",
    "Columbia University",
    "University of Tokyo",
    "London Business School",
    "University of Toronto",
    "HEC Paris",
    "University of Sydney",
    "Indian Institute of Management Bangalore",
    "University of Sao Paulo",
    "University of Amsterdam",
    "IE Business School",
]

GENDERS = ("female", "male", "nonbinary")
CAREER_SUFFIXES = (
    "market entry",
    "customer discovery",
    "platform operations",
    "pricing strategy",
    "partner enablement",
    "go-to-market planning",
    "workflow automation",
    "data quality",
    "enterprise pilots",
    "community activation",
    "compliance readiness",
    "international expansion",
)


def pick(items, user_number, multiplier=1):
    return items[((user_number - 1) * multiplier) % len(items)]


def parse_kinds(value):
    kinds = [item.strip() for item in (value or "").split(",") if item.strip()]
    invalid = [kind for kind in kinds if kind not in FIELD_CONFIGS]
    if invalid:
        raise CommandError(f"Invalid field kind(s): {', '.join(invalid)}")
    return kinds or list(DEFAULT_KINDS)


def field_value(user_number, kind):
    if kind == "email":
        return f"search.demo.{user_number:03d}@example.com"
    if kind == "mobile":
        return f"+8210555{user_number:04d}"
    if kind == "telegram":
        return f"@searchdemo{user_number:03d}"
    if kind == "discord":
        return f"searchdemo{user_number:03d}"
    if kind == "twitter":
        return f"@search_demo_{user_number:03d}"
    if kind == "insurance":
        return f"{pick(INSURANCE_PLANS, user_number)} / policy tier {1 + (user_number % 4)}"
    if kind == "height":
        return f"{158 + ((user_number * 3) % 35)} cm"
    if kind == "weight":
        return f"{48 + ((user_number * 5) % 45)} kg"
    if kind == "blood_type":
        return pick(BLOOD_TYPES, user_number)
    return f"search-demo-{user_number:03d}-{kind}"


def build_profile(user_number, id_prefix, wallet):
    user_id = f"{id_prefix}-{user_number:03d}"
    name, occupation, career_focus = pick(PERSONAS, user_number)
    country, residence = pick(LOCATIONS, user_number)
    education = pick(EDUCATIONS, user_number, multiplier=7)
    gender = pick(GENDERS, user_number)
    age = 24 + ((user_number * 7) % 22)
    career = f"{career_focus} / {pick(CAREER_SUFFIXES, user_number, multiplier=11)}"
    if user_number == 1:
        age = 29
    display_name = f"{name} {user_number:03d}"
    return {
        "id": user_id,
        "email": f"{user_id}@example.com",
        "displayName": display_name,
        "publicSlug": user_id,
        "walletAddress": wallet,
        "smartWalletAddress": wallet,
        "payoutAddress": wallet,
        "name": display_name,
        "gender": gender,
        "age": age,
        "country": country,
        "residence": residence,
        "occupation": occupation,
        "publicFields": {
            "name": display_name,
            "gender": gender,
            "age": age,
            "country": country,
            "locale": residence,
            "occupation": occupation,
        },
        "educations": [{"id": "education-1", "education": education, "status": "graduated"}],
        "careers": [{"id": "career-1", "career": career, "startDate": "2020-01", "endDate": "", "status": "employed"}],
    }


def build_field(profile, user_number, kind, wallet):
    config = FIELD_CONFIGS[kind]
    verification_status = "verified" if kind == "mobile" else "not_required"
    return {
        "id": f"{profile['id']}-{kind}",
        "userId": profile["id"],
        "profileId": profile["id"],
        "kind": kind,
        "label": config["label"],
        "valuePreview": field_value(user_number, kind),
        "accessMode": "paid",
        "priceCents": config["price"],
        "currency": "IP",
        "verificationStatus": verification_status,
        "cdrState": "off",
        "sellerAddress": wallet,
    }


def build_deploy_payload(field, metadata, wallet):
    return {
        "fieldId": field["id"],
        "profileId": field["userId"],
        "kind": field["kind"],
        "label": field["label"],
        "value": field["valuePreview"],
        "priceCents": field["priceCents"],
        "recipient": wallet,
        "ipMetadata": metadata["ipMetadata"],
    }


class Command(BaseCommand):
    help = "Create search-demo profiles/fields and optionally deploy real Story IPA + CDR rows."

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true", help="Write DB rows and run real IPA/CDR deployment.")
        parser.add_argument("--users", type=int, default=DEFAULT_USER_COUNT, help="Number of search-demo users to prepare.")
        parser.add_argument("--concurrency", type=int, default=1, help="Parallel live deployments. Keep at 1 for one server wallet.")
        parser.add_argument("--kinds", default=",".join(DEFAULT_KINDS), help="Comma-separated field kinds to prepare.")
        parser.add_argument("--id-prefix", default="search-demo", help="Stable ID prefix for generated users.")
        parser.add_argument("--recipient-wallet", default=DEFAULT_RECIPIENT_WALLET, help="Dummy user wallet and IPA recipient.")

    def handle(self, *args, **options):
        user_count = options["users"]
        concurrency = max(1, options["concurrency"])
        execute = bool(options["execute"])
        id_prefix = options["id_prefix"].strip() or "search-demo"
        wallet = options["recipient_wallet"].strip()
        kinds = parse_kinds(options["kinds"])

        if user_count < 1:
            raise CommandError("--users must be at least 1")
        if not ADDRESS_RE.fullmatch(wallet):
            raise CommandError("--recipient-wallet must be an EVM address")
        if execute and concurrency != 1:
            raise CommandError("--concurrency must be 1 for live CDR deployment with the shared server wallet")

        profiles = [build_profile(index, id_prefix, wallet) for index in range(1, user_count + 1)]
        jobs = []
        skipped = 0

        for index, profile in enumerate(profiles, start=1):
            field_specs = [build_field(profile, index, kind, wallet) for kind in kinds]
            if execute:
                repository.upsert_profile(profile)
            for field_spec in field_specs:
                existing = repository.get_fields_by_ids([field_spec["id"]])
                if existing and repository.field_has_mintable_license(existing[0]):
                    skipped += 1
                    self.stdout.write(f"skip mintable {field_spec['id']}")
                    continue
                if execute:
                    field = repository.upsert_field(field_spec)
                    jobs.append(field)
                else:
                    jobs.append(field_spec)

        if not execute:
            self.stdout.write(
                f"DRY RUN: users={user_count} kinds={','.join(kinds)} deploy_candidates={len(jobs)} skipped_mintable={skipped}"
            )
            return

        succeeded = 0
        failed = 0
        if jobs:
            if concurrency == 1:
                for field in jobs:
                    ok = self.deploy_and_report(field, wallet)
                    succeeded += 1 if ok else 0
                    failed += 0 if ok else 1
            else:
                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    futures = {executor.submit(self.deploy_field, field, wallet): field for field in jobs}
                    for future in as_completed(futures):
                        field = futures[future]
                        try:
                            saved = future.result()
                        except Exception as error:
                            failed += 1
                            self.stderr.write(f"failed {field['id']}: {error}")
                            continue
                        if self.report_saved_field(field, saved):
                            succeeded += 1
                        else:
                            failed += 1

        self.stdout.write(
            f"complete users={user_count} fields={len(jobs)} deployed={succeeded} failed={failed} skipped_mintable={skipped}"
        )

    def deploy_field(self, field, wallet):
        close_old_connections()
        try:
            metadata = upload_field_ip_metadata(field["userId"], field["kind"], field["label"])
            deployment = deploy_field_cdr_with_server_wallet(build_deploy_payload(field, metadata, wallet))
            return repository.save_server_cdr_deployment(field["id"], deployment)
        finally:
            close_old_connections()

    def deploy_and_report(self, field, wallet):
        try:
            saved = self.deploy_field(field, wallet)
        except Exception as error:
            self.stderr.write(f"failed {field['id']}: {error}")
            return False
        return self.report_saved_field(field, saved)

    def report_saved_field(self, field, saved):
        if saved and repository.field_has_mintable_license(saved):
            self.stdout.write(f"deployed {saved['id']} ipa={saved.get('cdrLicenseIpId')} cdr={saved.get('cdrVaultUuid')}")
            return True
        self.stderr.write(f"failed {field['id']}: deployment_saved_but_not_mintable")
        return False
