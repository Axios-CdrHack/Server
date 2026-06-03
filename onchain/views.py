import re

from django.http import StreamingHttpResponse
from django.views.decorators.http import require_http_methods

from main.constants import PURCHASE_CONTRACT_ADDRESS, STORY_AENEID_RPC_URL
from main.errors import ApiError, ValidationApiError
from main.views import (
    api_endpoint,
    api_error_payload,
    assert_field_auth,
    assert_profile_auth,
    assert_wallet_auth,
    authenticated,
    json_ok,
    parse_json,
    require_keys,
    sse_message,
    validate_address,
    validate_tx_hash,
    wallet_matches_auth,
)
from onchain import repository
from onchain.integrations import deploy_field_cdr_with_server_wallet, list_onchain_sales_by_wallet, upload_field_ip_metadata

UINT_RE = re.compile(r"^\d+$")


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def field_ip_metadata(request):
    body = parse_json(request)
    require_keys(body, ["profileId", "kind"])
    if body["kind"] not in {"email", "mobile", "telegram", "discord", "twitter"}:
        raise ValidationApiError(issues=[{"path": ["kind"], "message": "Invalid field kind"}])
    assert_profile_auth(request.app_auth, body["profileId"])
    metadata = upload_field_ip_metadata(body["profileId"], body["kind"], str(body.get("label") or body["kind"]))
    return json_ok({"metadata": metadata}, status=201)


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def cdr_deploy_log(request):
    body = parse_json(request)
    require_keys(body, ["fieldId", "cdrVaultUuid", "deployTxHash"])
    if not UINT_RE.fullmatch(str(body["cdrVaultUuid"])):
        raise ValidationApiError(issues=[{"path": ["cdrVaultUuid"], "message": "Invalid CDR uuid"}])
    validate_tx_hash(body["deployTxHash"], "deployTxHash")
    assert_field_auth(request.app_auth, body["fieldId"])
    field = repository.save_deploy_log(body["fieldId"], body["cdrVaultUuid"], body["deployTxHash"])
    if not field:
        return json_ok({"error": "field_not_deployable"}, status=400)
    return json_ok({"field": field}, status=201)


def build_cdr_server_deploy_payload(app_auth, field_id):
    assert_field_auth(app_auth, field_id)
    fields = repository.get_fields_by_ids([field_id])
    field = fields[0] if fields else None
    if not field:
        raise ApiError("field_not_found", status_code=404)
    if not repository.field_can_start_cdr_deploy(field):
        raise ApiError("field_not_deployable", status_code=400)
    has_existing_deployment = bool(
        field.get("cdrVaultUuid")
        and field.get("deployTxHash")
        and field.get("cdrLicenseIpId")
        and field.get("cdrLicenseTermsId")
    )
    if has_existing_deployment:
        raise ApiError("field_already_issued", status_code=409)

    profile = repository.get_profile(field["userId"])
    recipient = (profile.get("walletAddress") if profile else None) or app_auth.get("walletAddress")
    recipient = validate_address(recipient, "recipient")
    if not wallet_matches_auth(app_auth, recipient):
        raise ApiError("wallet_not_authorized", status_code=403)

    return field, {
        "fieldId": field["id"],
        "profileId": field["userId"],
        "kind": field["kind"],
        "label": field["label"],
        "value": field["valuePreview"],
        "priceCents": field["priceCents"],
        "recipient": recipient,
    }


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def cdr_server_deploy(request):
    body = parse_json(request)
    require_keys(body, ["fieldId"])
    field, payload = build_cdr_server_deploy_payload(request.app_auth, body["fieldId"])
    metadata = upload_field_ip_metadata(field["userId"], field["kind"], field["label"])
    payload["ipMetadata"] = metadata["ipMetadata"]
    deployment = deploy_field_cdr_with_server_wallet(payload)
    saved = repository.save_server_cdr_deployment(field["id"], deployment)
    if not saved:
        return json_ok({"error": "field_not_deployable"}, status=400)
    return json_ok({"field": saved, "deployment": deployment}, status=201)


@api_endpoint
@authenticated
@require_http_methods(["GET"])
def cdr_server_deploy_events(request):
    field_id = request.GET.get("fieldId")
    if not field_id:
        raise ValidationApiError(issues=[{"path": ["fieldId"], "message": "Required"}])
    field, payload = build_cdr_server_deploy_payload(request.app_auth, field_id)

    def stream():
        try:
            yield sse_message("status", {"status": "metadata", "message": "Preparing metadata"})
            metadata = upload_field_ip_metadata(field["userId"], field["kind"], field["label"])
            payload["ipMetadata"] = metadata["ipMetadata"]

            yield sse_message("status", {"status": "deploying", "message": "Server minting IPA + CDR"})
            deployment = deploy_field_cdr_with_server_wallet(payload)

            yield sse_message("status", {"status": "saving", "message": "Saving deployment"})
            saved = repository.save_server_cdr_deployment(field["id"], deployment)
            if not saved:
                yield sse_message("error", {"error": "field_not_deployable"})
                return

            yield sse_message("complete", {"status": "complete", "field": saved, "deployment": deployment})
        except Exception as error:
            yield sse_message("error", api_error_payload(error))

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@api_endpoint
@authenticated
@require_http_methods(["POST"])
def cdr_toggle(request):
    body = parse_json(request)
    require_keys(body, ["fieldId", "cdrState"])
    if body["cdrState"] not in {"off", "deploying", "on"}:
        raise ValidationApiError(issues=[{"path": ["cdrState"], "message": "Invalid CDR state"}])
    assert_field_auth(request.app_auth, body["fieldId"])
    field = repository.set_cdr_state(body["fieldId"], body["cdrState"])
    if not field:
        return json_ok({"error": "field_not_toggleable"}, status=400)
    return json_ok({"field": field})


def merge_sales(server_sales, onchain_sales):
    sales = {}
    for sale in server_sales + onchain_sales:
        key = f"{sale.get('paymentTxHash')}:{sale.get('fieldId')}" if sale.get("paymentTxHash") else sale["id"]
        sales[key] = sale
    return sorted(sales.values(), key=lambda item: item.get("createdAt", ""), reverse=True)


@api_endpoint
@authenticated
@require_http_methods(["GET"])
def sales(request):
    wallet = validate_address(request.GET.get("wallet"), "wallet")
    assert_wallet_auth(request.app_auth, wallet)
    server_sales = repository.list_sales_by_wallet(wallet)
    onchain_sales = list_onchain_sales_by_wallet(wallet)
    return json_ok(
        {
            "sales": merge_sales(server_sales, onchain_sales),
            "onchain": {"rpcUrl": STORY_AENEID_RPC_URL, "contract": PURCHASE_CONTRACT_ADDRESS, "logCount": len(onchain_sales)},
        }
    )
