import importlib
import os
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.apps import apps as django_apps
from django.test import Client, TestCase

from . import auth as auth_module
from .auth import get_privy_user, sign_app_jwt, verify_privy_access_token
from .models import StoreRecord
from data.models import AppDataField, AppOrder, AppOrderItem, AppOrderSellerPayout, AppQuote
from data.integrations import hex_with_0x
from data.orders import build_access_aux_data
from onchain.models import AppCdrVault
from users.models import AppCareer, AppEducation, AppUser


class ApiSmokeTests(TestCase):
    def setUp(self):
        os.environ["PRIVY_APP_SECRET"] = "test-secret"
        self.client = Client(HTTP_ORIGIN="http://localhost:3000")

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

    @patch("api.auth.requests.get")
    def test_privy_user_lookup_sends_app_id_header(self, mock_get):
        os.environ["PRIVY_APP_ID"] = "test-privy-app-id"
        os.environ["PRIVY_APP_SECRET"] = "test-privy-secret"
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"id": "did:privy:test", "linked_accounts": []}

        get_privy_user("did:privy:test")

        headers = mock_get.call_args.kwargs["headers"]
        self.assertEqual(headers["privy-app-id"], "test-privy-app-id")

    @patch("api.auth.requests.get")
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


class StoreRecordMigrationTests(TestCase):
    def test_store_records_migrate_into_domain_tables(self):
        StoreRecord.objects.create(
            namespace="users",
            key="user-x",
            value={
                "id": "user-x",
                "email": "owner@example.com",
                "walletAddress": "0x1111111111111111111111111111111111111111",
                "displayName": "Owner",
                "publicSlug": "owner-card",
                "createdAt": "2026-05-21T00:00:00.000Z",
                "updatedAt": "2026-05-21T00:00:00.000Z",
            },
        )
        StoreRecord.objects.create(
            namespace="profiles",
            key="user-x",
            value={
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
        StoreRecord.objects.create(
            namespace="data_fields",
            key="field-x",
            value={
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
        StoreRecord.objects.create(
            namespace="quotes",
            key="quote-x",
            value={
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
        StoreRecord.objects.create(
            namespace="orders",
            key="order-x",
            value={
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
