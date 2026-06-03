import importlib
import json
import os
from io import StringIO
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.apps import apps as django_apps
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, TestCase

from . import auth as auth_module
from .auth import get_privy_user, sign_app_jwt, verify_privy_access_token
from data import repository
from data.models import AppDataField, AppOrder, AppOrderItem, AppOrderSellerPayout, AppQuote, AppSearchDocument
from data.integrations import hex_with_0x
from data.orders import build_access_aux_data, create_order, get_export_plan
from onchain.models import AppCdrVault
from users.models import AppCareer, AppEducation, AppUser


class ApiSmokeTests(TestCase):
    def setUp(self):
        os.environ["PRIVY_APP_SECRET"] = "test-secret"
        self.client = Client(HTTP_ORIGIN="http://localhost:3000")
        self.create_profiles()

    def create_profiles(self):
        profiles = [
            ("user-1", "Jaine", "jaine-k7q3p9", "jane.pm@example.com", "0x1111111111111111111111111111111111111111", "female", 30, "Korea", "Seoul", "IT Product Manager"),
            ("user-2", "Platform Operator", "platform-operator-9d2hvk", "ops.tpm@example.com", "0x2222222222222222222222222222222222222222", "nonbinary", 31, "Singapore", "Singapore", "Technical Program Manager"),
            ("user-3", "Fintech Maker", "fintech-maker-v3r8n2", "fintech.pm@example.com", "0x3333333333333333333333333333333333333333", "male", 29, "United States", "New York", "Product Manager"),
            ("user-4", "Growth PM", "growth-pm-h8t4kc", "growth.pm@example.com", "0x5555555555555555555555555555555555555555", "female", 32, "Germany", "Berlin", "Growth Product Lead"),
            ("user-5", "Enterprise Seller", "sales-lead-z2n6qa", "sales.manager@example.com", "0x4444444444444444444444444444444444444444", "male", 34, "United States", "San Francisco", "Sales Manager"),
        ]
        for user_id, name, slug, email, wallet, gender, age, country, residence, occupation in profiles:
            user = AppUser.objects.create(
                id=user_id,
                email=email,
                wallet_address=wallet,
                payout_address=wallet,
                name=name,
                age=age,
                occupation=occupation,
                gender=gender,
                country=country,
                residence=residence,
                display_name=name,
                public_slug=slug,
                has_profile=True,
            )
            AppEducation.objects.create(id=f"{user_id}-education-1", user=user, education="Yonsei University", status="graduated")
            AppCareer.objects.create(id=f"{user_id}-career-1", user=user, career=occupation, start_date="2020-01", end_date="", status="employed")
            if user_id == "user-1":
                for kind, price in {"email": 900, "mobile": 1400, "telegram": 450, "discord": 350, "twitter": 300}.items():
                    AppDataField.objects.create(
                        id=f"{user_id}-{kind}",
                        user=user,
                        kind=kind,
                        label="Mobile" if kind == "mobile" else kind[:1].upper() + kind[1:],
                        value_preview=email if kind == "email" else f"@{name.lower().replace(' ', '')}",
                        access_mode="paid",
                        price_cents=price,
                        currency="IP",
                        requires_verification=kind == "mobile",
                        verification_status="verified" if kind == "mobile" else "not_required",
                        cdr_state="off",
                        seller_address=wallet,
                    )

    def auth_headers(self):
        token = sign_app_jwt(
            {
                "privyUserId": "privy-test",
                "sessionId": "session-test",
                "expiresAtSeconds": 9999999999,
                "email": "jane.pm@example.com",
                "walletAddress": "0x1111111111111111111111111111111111111111",
            }
        )["token"]
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def tx_hash(self, seed):
        return "0x" + seed * 64

    def make_field_mintable(self, field_id, index):
        field = AppDataField.objects.get(id=field_id)
        field.cdr_state = "on"
        field.save(update_fields=["cdr_state"])
        AppCdrVault.objects.create(
            id=f"{field_id}-cdr",
            field=field,
            cdr_vault_uuid=str(100 + index),
            owner_address="0x9999999999999999999999999999999999999999",
            write_condition_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            read_condition_address="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            write_condition_data="0x12",
            read_condition_data="0x34",
            deploy_tx_hash=self.tx_hash("a"),
            cdr_license_ip_id="0x" + f"{index:040x}",
            cdr_license_terms_id="123",
            ipa_nft_contract="0x3333333333333333333333333333333333333333",
            ipa_token_id=str(index),
            license_config_tx_hash=self.tx_hash("b"),
            status="active",
        )
        return field

    def test_public_health_profiles_and_card(self):
        self.assertEqual(self.client.get("/health").json()["ok"], True)
        profiles = self.client.get("/profiles").json()["profiles"]
        self.assertEqual(len(profiles), 5)
        self.assertEqual(self.client.get("/c/jaine-k7q3p9").status_code, 200)

    def test_owner_can_read_fields_and_toggle_cdr(self):
        response = self.client.get("/profiles/user-1/fields", **self.auth_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["fields"]), 5)

        response = self.client.post(
            "/cdr/toggle",
            {"fieldId": "user-1-twitter", "cdrState": "off"},
            content_type="application/json",
            **self.auth_headers(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["field"]["cdrState"], "off")

    def test_owner_can_upsert_profile(self):
        response = self.client.post(
            "/profiles",
            {
                "id": "user-1",
                "displayName": "Jane Updated",
                "publicFields": {"name": "Jane Updated", "gender": "female", "age": 31, "country": "Korea"},
            },
            content_type="application/json",
            **self.auth_headers(),
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["profile"]["publicFields"]["name"], "Jane Updated")

    def test_upsert_profile_requires_auth(self):
        response = self.client.post(
            "/profiles",
            {"displayName": "x", "publicFields": {"name": "x", "gender": "male", "age": 30}},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_access_aux_data_encodes_license_token_ids(self):
        encoded = build_access_aux_data({"licenseTokenIds": ["1", "2"]})
        self.assertTrue(encoded.startswith("0x"))
        self.assertIn("0000000000000000000000000000000000000000000000000000000000000002", encoded)

    @patch("data.orders.verify_license_tokens_owned_by")
    def test_order_accepts_batch_license_mint_tx_hash(self, mock_verify):
        buyer = "0x4444444444444444444444444444444444444444"
        self.make_field_mintable("user-1-email", 1)
        self.make_field_mintable("user-1-telegram", 2)
        AppQuote.objects.create(
            id="quote-batch",
            buyer_wallet=buyer,
            prompt="pm in seoul",
            filters={"country": "Korea", "terms": ["pm"]},
            recommended_fields=["email", "telegram"],
            wanted_fields=["email", "telegram"],
            profile_ids=["user-1"],
            matched_profile_count=1,
        )
        batch_tx_hash = self.tx_hash("c")

        order = create_order(
            {
                "quoteId": "quote-batch",
                "buyerWallet": buyer,
                "prompt": "pm in seoul",
                "wantedFields": ["email", "telegram"],
                "selectedFieldIds": ["user-1-email", "user-1-telegram"],
                "licenseTokenGrants": [
                    {"fieldId": "user-1-email", "licenseTokenId": "101", "mintTxHash": batch_tx_hash},
                    {"fieldId": "user-1-telegram", "licenseTokenId": "102", "mintTxHash": batch_tx_hash},
                ],
                "paymentTxHash": batch_tx_hash,
            },
            owner_wallets=[buyer],
        )

        self.assertEqual(order["paymentTxHash"], batch_tx_hash)
        self.assertEqual(order["licenseTokenIds"], ["101", "102"])
        self.assertEqual({grant["mintTxHash"] for grant in order["licenseTokenGrants"]}, {batch_tx_hash})
        self.assertEqual(AppOrderItem.objects.filter(order_id=order["id"]).count(), 2)
        mock_verify.assert_called_once_with([buyer], ["101", "102"])

        export_plan = get_export_plan(order["id"])
        aux_by_field = {item["fieldId"]: item["accessAuxData"] for item in export_plan["items"]}
        self.assertEqual(aux_by_field["user-1-email"], build_access_aux_data({"licenseTokenIds": ["101"]}))
        self.assertEqual(aux_by_field["user-1-telegram"], build_access_aux_data({"licenseTokenIds": ["102"]}))

    def test_rpc_hex_values_keep_0x_prefix(self):
        self.assertEqual(hex_with_0x("abc123"), "0xabc123")
        self.assertEqual(hex_with_0x("0xabc123"), "0xabc123")

    @patch("data.integrations.requests.post")
    def test_mobile_verification_sends_twilio_sms(self, mock_post):
        os.environ["TWILIO_ACCOUNT_SID"] = "ACtestsid"
        os.environ["TWILIO_AUTH_TOKEN"] = "test-token"
        os.environ["TWILIO_FROM_NUMBER"] = "+15550000000"
        mock_post.return_value.status_code = 201

        response = self.client.post(
            "/verify/start",
            {"profileId": "user-1", "channel": "mobile", "target": "+82105551234"},
            content_type="application/json",
            **self.auth_headers(),
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["verificationId"].startswith("verify-"))
        self.assertEqual(
            mock_post.call_args.args[0],
            "https://api.twilio.com/2010-04-01/Accounts/ACtestsid/Messages.json",
        )
        self.assertEqual(mock_post.call_args.kwargs["auth"], ("ACtestsid", "test-token"))
        self.assertEqual(mock_post.call_args.kwargs["data"]["To"], "+82105551234")
        self.assertEqual(mock_post.call_args.kwargs["data"]["From"], "+15550000000")
        self.assertIn("Your AXIOS verification code is", mock_post.call_args.kwargs["data"]["Body"])

    @patch("main.auth.requests.get")
    def test_privy_user_lookup_sends_app_id_header(self, mock_get):
        os.environ["PRIVY_APP_ID"] = "test-privy-app-id"
        os.environ["PRIVY_APP_SECRET"] = "test-privy-secret"
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"id": "did:privy:test", "linked_accounts": []}

        get_privy_user("did:privy:test")

        headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(headers["privy-app-id"], "test-privy-app-id")

    @patch("main.auth.requests.get")
    def test_privy_access_token_uses_app_verification_key(self, mock_get):
        auth_module._PRIVY_VERIFICATION_KEY_CACHE.clear()
        os.environ["PRIVY_APP_ID"] = "test-privy-app-id"
        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_pem = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        token = jwt.encode(
            {
                "sid": "session-test",
                "sub": "did:privy:test",
                "iss": "privy.io",
                "aud": "test-privy-app-id",
                "iat": 1,
                "exp": 9999999999,
            },
            private_pem,
            algorithm="ES256",
        )
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"verification_key": "".join(public_pem.splitlines())}

        verified = verify_privy_access_token(token)

        self.assertEqual(verified["user_id"], "did:privy:test")
        self.assertEqual(verified["session_id"], "session-test")
        self.assertEqual(verified["app_id"], "test-privy-app-id")
        self.assertEqual(mock_get.call_args.args[0], "https://auth.privy.io/api/v1/apps/test-privy-app-id")
        self.assertEqual(mock_get.call_args.kwargs["headers"]["privy-app-id"], "test-privy-app-id")


class BulkSearchCdrCommandTests(TestCase):
    wallet = "0xE34CD8C9C1561A575a24bE5A5E0e883D96E28f81"
    platform_wallet = "0x9999999999999999999999999999999999999999"

    def tx_hash(self, seed):
        return "0x" + f"{seed:064x}"

    def mock_metadata(self, profile_id, kind, label):
        return {
            "ipMetadata": {
                "ipMetadataURI": f"https://metadata.example/{profile_id}/{kind}.json",
                "ipMetadataHash": self.tx_hash(9000),
                "nftMetadataURI": f"https://metadata.example/{profile_id}/{kind}.json",
                "nftMetadataHash": self.tx_hash(9001),
            }
        }

    def mock_deployment(self, payload):
        kind_index = {
            "email": 1,
            "mobile": 2,
            "telegram": 3,
            "discord": 4,
            "twitter": 5,
            "insurance": 6,
            "height": 7,
            "weight": 8,
            "blood_type": 9,
        }[payload["kind"]]
        user_number = int(payload["fieldId"].split("-")[-2])
        seed = user_number * 10 + kind_index
        return {
            "platformWallet": self.platform_wallet,
            "recipient": payload["recipient"],
            "cdrVaultUuid": str(5000 + seed),
            "deployTxHash": self.tx_hash(seed),
            "allocateTxHash": self.tx_hash(1000 + seed),
            "cdrOwnerAddress": self.platform_wallet,
            "writeConditionAddress": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "readConditionAddress": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "writeConditionData": "0x12",
            "readConditionData": "0x34",
            "cdrLicenseIpId": "0x" + f"{seed:040x}",
            "cdrLicenseTermsId": "1894",
            "ipaNftContract": "0xcccccccccccccccccccccccccccccccccccccccc",
            "ipaTokenId": str(seed),
            "ipRegistrationTxHash": self.tx_hash(2000 + seed),
            "licenseConfigTxHash": self.tx_hash(3000 + seed),
            "licenseAttachTxHash": self.tx_hash(3000 + seed),
            "ipaTransferTxHash": self.tx_hash(4000 + seed),
        }

    def test_bulk_command_default_dry_run_prepares_100_profiles(self):
        out = StringIO()

        call_command("bulk_search_cdr", stdout=out)

        self.assertIn("users=100", out.getvalue())
        self.assertIn("deploy_candidates=900", out.getvalue())
        self.assertEqual(AppUser.objects.count(), 0)

    def test_bulk_command_rejects_parallel_live_deployments(self):
        with self.assertRaisesMessage(CommandError, "--concurrency must be 1"):
            call_command("bulk_search_cdr", "--execute", "--users", "1", "--kinds", "email", "--concurrency", "2", stdout=StringIO())

    def test_bulk_profiles_use_diverse_template_combinations(self):
        from data.management.commands.bulk_search_cdr import build_profile

        profiles = [build_profile(index, "search-demo", self.wallet) for index in range(1, 101)]

        self.assertGreaterEqual(len({profile["occupation"] for profile in profiles}), 20)
        self.assertGreaterEqual(len({profile["residence"] for profile in profiles}), 15)
        self.assertGreaterEqual(len({profile["educations"][0]["education"] for profile in profiles}), 15)
        self.assertEqual(profiles[0]["country"], "Korea")
        self.assertEqual(profiles[0]["occupation"], "Product Manager")

    @patch("data.management.commands.bulk_search_cdr.deploy_field_cdr_with_server_wallet")
    @patch("data.management.commands.bulk_search_cdr.upload_field_ip_metadata")
    def test_bulk_command_creates_searchable_deployed_fields_and_skips_rerun(self, mock_upload, mock_deploy):
        mock_upload.side_effect = self.mock_metadata
        mock_deploy.side_effect = self.mock_deployment
        out = StringIO()

        call_command("bulk_search_cdr", "--execute", "--users", "2", "--kinds", "email,insurance,height,blood_type", "--concurrency", "1", stdout=out)

        self.assertEqual(AppUser.objects.count(), 2)
        self.assertEqual(AppSearchDocument.objects.count(), 2)
        self.assertEqual(AppDataField.objects.count(), 8)
        self.assertEqual(AppCdrVault.objects.count(), 8)
        user = AppUser.objects.get(id="search-demo-001")
        self.assertEqual(user.wallet_address, self.wallet)
        self.assertEqual(user.smart_wallet_address, self.wallet)
        self.assertEqual(user.payout_address, self.wallet)
        field = AppDataField.objects.get(id="search-demo-001-email")
        self.assertEqual(field.seller_address, self.wallet)
        self.assertEqual(field.cdr_state, "on")
        vault = AppCdrVault.objects.get(field=field)
        self.assertEqual(vault.owner_address, self.platform_wallet)
        self.assertEqual(vault.ipa_recipient, self.wallet)
        self.assertEqual(vault.write_condition_data, "0x12")
        self.assertEqual(vault.read_condition_data, "0x34")
        self.assertTrue(repository.field_has_mintable_license(repository.field_to_dict(field)))

        self.assertEqual(AppDataField.objects.get(id="search-demo-001-insurance").label, "Insurance Data")
        self.assertEqual(AppDataField.objects.get(id="search-demo-001-blood_type").value_preview, "**")

        call_command("bulk_search_cdr", "--execute", "--users", "2", "--kinds", "email,insurance,height,blood_type", "--concurrency", "1", stdout=StringIO())

        self.assertEqual(mock_deploy.call_count, 8)
        self.assertEqual(AppDataField.objects.count(), 8)

    @patch("data.management.commands.bulk_search_cdr.deploy_field_cdr_with_server_wallet")
    @patch("data.management.commands.bulk_search_cdr.upload_field_ip_metadata")
    @patch("data.search.analyze_search_intent")
    def test_bulk_command_fields_match_search_quote(self, mock_intent, mock_upload, mock_deploy):
        mock_upload.side_effect = self.mock_metadata
        mock_deploy.side_effect = self.mock_deployment
        mock_intent.return_value = {
            "filters": {"country": "Korea", "terms": ["product"]},
            "recommendedFields": ["email", "insurance", "blood_type"],
        }
        call_command("bulk_search_cdr", "--execute", "--users", "2", "--kinds", "email,insurance,blood_type", "--concurrency", "1", stdout=StringIO())

        response = Client(HTTP_ORIGIN="http://localhost:3000").post(
            "/search/quote",
            {"prompt": "korea product contacts", "wantedFields": ["email", "insurance", "blood_type"]},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["matchedProfileCount"], 1)
        self.assertEqual(payload["paidFieldCount"], 3)
        costs = payload["matches"][0]["fieldCosts"]
        self.assertEqual({item["kind"] for item in costs}, {"email", "insurance", "blood_type"})
        self.assertTrue(all(item.get("cdrLicenseIpId") for item in costs))
        self.assertTrue(all(item.get("ipaTokenId") for item in costs))


class LegacyStoreMigrationTests(TestCase):
    def insert_store_record(self, namespace, key, value):
        with connection.cursor() as cursor:
            cursor.execute(
                "insert into api_storerecord (namespace, key, value, updated_at) values (%s, %s, %s, %s)",
                [namespace, key, json.dumps(value), "2026-05-21 00:00:00"],
            )

    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                create table api_storerecord (
                    id integer primary key autoincrement,
                    namespace varchar(64) not null,
                    key varchar(160) not null,
                    value text not null,
                    updated_at datetime not null
                )
                """
            )

    def test_legacy_store_rows_migrate_into_domain_tables(self):
        self.insert_store_record(
            "users",
            "user-x",
            {
                "id": "user-x",
                "email": "owner@example.com",
                "walletAddress": "0x1111111111111111111111111111111111111111",
                "displayName": "Owner",
                "publicSlug": "owner-card",
                "createdAt": "2026-05-21T00:00:00.000Z",
                "updatedAt": "2026-05-21T00:00:00.000Z",
            },
        )
        self.insert_store_record(
            "profiles",
            "user-x",
            {
                "id": "user-x",
                "email": "owner@example.com",
                "walletAddress": "0x1111111111111111111111111111111111111111",
                "payoutAddress": "0x1111111111111111111111111111111111111111",
                "displayName": "Owner",
                "publicSlug": "owner-card",
                "name": "Owner",
                "age": 30,
                "gender": "female",
                "country": "Korea",
                "residence": "Seoul",
                "occupation": "Product Manager",
                "educations": [{"id": "education-1", "education": "Yonsei", "status": "graduated"}],
                "careers": [{"id": "career-1", "career": "PM", "startDate": "2020-01", "endDate": "", "status": "employed"}],
                "createdAt": "2026-05-21T00:00:00.000Z",
                "updatedAt": "2026-05-21T00:00:00.000Z",
            },
        )
        self.insert_store_record(
            "data_fields",
            "field-x",
            {
                "id": "field-x",
                "userId": "user-x",
                "kind": "email",
                "label": "Email",
                "valuePreview": "owner@example.com",
                "accessMode": "paid",
                "priceCents": 900,
                "currency": "IP",
                "requiresVerification": False,
                "verificationStatus": "not_required",
                "cdrState": "on",
                "cdrVaultUuid": "42",
                "deployTxHash": "0x" + "a" * 64,
                "cdrLicenseIpId": "0x2222222222222222222222222222222222222222",
                "cdrLicenseTermsId": "123",
                "ipaNftContract": "0x3333333333333333333333333333333333333333",
                "ipaTokenId": "7",
                "licenseConfigTxHash": "0x" + "b" * 64,
                "sellerAddress": "0x1111111111111111111111111111111111111111",
                "createdAt": "2026-05-21T00:00:00.000Z",
                "updatedAt": "2026-05-21T00:00:00.000Z",
            },
        )
        self.insert_store_record(
            "quotes",
            "quote-x",
            {
                "id": "quote-x",
                "buyerWallet": "0x4444444444444444444444444444444444444444",
                "prompt": "pm in seoul",
                "filters": {"country": "Korea", "terms": ["pm"]},
                "recommendedFields": ["email"],
                "wantedFields": ["email"],
                "profileIds": ["user-x"],
                "createdAt": "2026-05-21T00:00:00.000Z",
            },
        )
        self.insert_store_record(
            "orders",
            "order-x",
            {
                "id": "order-x",
                "quoteId": "quote-x",
                "buyerWallet": "0x4444444444444444444444444444444444444444",
                "prompt": "pm in seoul",
                "filters": {"country": "Korea", "terms": ["pm"]},
                "selectedProfileIds": ["user-x"],
                "selectedMatchRefs": ["match-1"],
                "selectedFieldIds": ["field-x"],
                "subtotalCents": 900,
                "serviceFeeCents": 0,
                "totalCents": 900,
                "currency": "IP",
                "status": "paid",
                "paymentTxHash": "0x" + "c" * 64,
                "licenseTokenIds": ["77"],
                "licenseTokenGrants": [{"fieldId": "field-x", "licenseTokenId": "77", "mintTxHash": "0x" + "d" * 64}],
                "purchaseContract": "0x5555555555555555555555555555555555555555",
                "accessProof": "0x" + "e" * 64,
                "sellerPayouts": [
                    {
                        "sellerAddress": "0x1111111111111111111111111111111111111111",
                        "fieldIds": ["field-x"],
                        "grossCents": 900,
                        "sellerCents": 900,
                        "serviceFeeCents": 0,
                    }
                ],
                "createdAt": "2026-05-21T00:00:00.000Z",
                "updatedAt": "2026-05-21T00:00:00.000Z",
            },
        )

        migration = importlib.import_module("data.migrations.0002_migrate_store_records")
        migration.migrate_store_records(django_apps, None)

        self.assertTrue(AppUser.objects.get(id="user-x").has_profile)
        self.assertEqual(AppEducation.objects.get(user_id="user-x").id, "user-x-education-1")
        self.assertEqual(AppCareer.objects.get(user_id="user-x").id, "user-x-career-1")
        self.assertEqual(AppDataField.objects.get(id="field-x").user_id, "user-x")
        self.assertEqual(AppCdrVault.objects.get(field_id="field-x").cdr_license_terms_id, "123")
        self.assertEqual(AppQuote.objects.get(id="quote-x").profile_ids, ["user-x"])
        self.assertEqual(AppOrder.objects.get(id="order-x").selected_field_ids, ["field-x"])
        self.assertEqual(AppOrderItem.objects.get(order_id="order-x").license_token_id, "77")
        self.assertEqual(AppOrderSellerPayout.objects.get(order_id="order-x").field_ids, ["field-x"])
