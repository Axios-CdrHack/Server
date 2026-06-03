from django.db import models
from django.utils import timezone


class AppDataField(models.Model):
    id = models.CharField(max_length=120, primary_key=True)
    user = models.ForeignKey("users.AppUser", related_name="data_fields", on_delete=models.CASCADE)
    kind = models.CharField(max_length=24)
    label = models.CharField(max_length=120)
    value_preview = models.TextField(blank=True, default="")
    access_mode = models.CharField(max_length=16, default="free")
    price_cents = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=8, default="IP")
    requires_verification = models.BooleanField(default=False)
    verification_status = models.CharField(max_length=24, default="not_required")
    cdr_state = models.CharField(max_length=16, default="off")
    seller_address = models.CharField(max_length=64, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_data_fields"
        constraints = [
            models.UniqueConstraint(fields=["user", "kind"], name="app_field_user_kind_unique"),
        ]
        indexes = [
            models.Index(fields=["user", "kind"], name="app_field_user_kind_idx"),
            models.Index(fields=["kind", "price_cents"], name="app_field_kind_price_idx"),
            models.Index(fields=["seller_address"], name="app_field_seller_idx"),
        ]


class AppSearchDocument(models.Model):
    user = models.OneToOneField("users.AppUser", primary_key=True, related_name="search_document", on_delete=models.CASCADE)
    age = models.PositiveSmallIntegerField(default=0)
    gender = models.CharField(max_length=40, blank=True, default="")
    country = models.CharField(max_length=80, blank=True, default="Korea")
    residence = models.CharField(max_length=120, blank=True, default="")
    occupation = models.CharField(max_length=160, blank=True, default="")
    tags = models.JSONField(default=list)
    searchable_text = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_search_documents"
        indexes = [
            models.Index(fields=["age"], name="app_search_age_idx"),
            models.Index(fields=["country", "residence"], name="app_search_country_idx"),
        ]


class AppQuote(models.Model):
    id = models.CharField(max_length=120, primary_key=True)
    buyer_wallet = models.CharField(max_length=64, blank=True, default="")
    prompt = models.TextField(blank=True, default="")
    filters = models.JSONField(default=dict)
    recommended_fields = models.JSONField(default=list)
    wanted_fields = models.JSONField(default=list)
    profile_ids = models.JSONField(default=list)
    matches = models.JSONField(default=list)
    extensions = models.JSONField(default=list)
    matched_profile_count = models.PositiveIntegerField(default=0)
    paid_field_count = models.PositiveIntegerField(default=0)
    free_field_count = models.PositiveIntegerField(default=0)
    subtotal_cents = models.PositiveIntegerField(default=0)
    service_fee_cents = models.PositiveIntegerField(default=0)
    total_cents = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=8, default="IP")
    batch_size = models.PositiveIntegerField(default=100)
    capped = models.BooleanField(default=False)
    max_paid_fields = models.PositiveIntegerField(default=800)
    sheet_params = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_quotes"
        indexes = [
            models.Index(fields=["buyer_wallet", "-created_at"], name="app_quote_buyer_idx"),
        ]


class AppOrder(models.Model):
    id = models.CharField(max_length=120, primary_key=True)
    quote = models.ForeignKey(AppQuote, null=True, blank=True, on_delete=models.SET_NULL)
    buyer_wallet = models.CharField(max_length=64)
    prompt = models.TextField(blank=True, default="")
    filters = models.JSONField(default=dict)
    selected_profile_ids = models.JSONField(default=list)
    selected_match_refs = models.JSONField(default=list)
    selected_field_ids = models.JSONField(default=list)
    subtotal_cents = models.PositiveIntegerField(default=0)
    service_fee_cents = models.PositiveIntegerField(default=0)
    total_cents = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=8, default="IP")
    batch_size = models.PositiveIntegerField(default=100)
    platform_fee_bps = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=32, default="pending_payment")
    payment_tx_hash = models.CharField(max_length=96, blank=True, null=True)
    license_token_ids = models.JSONField(default=list)
    license_token_grants = models.JSONField(default=list)
    purchase_contract = models.CharField(max_length=64, blank=True, default="")
    access_proof = models.CharField(max_length=96, blank=True, default="")
    sheet_params = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_orders"
        indexes = [
            models.Index(fields=["buyer_wallet", "-created_at"], name="app_order_buyer_idx"),
            models.Index(fields=["status", "-created_at"], name="app_order_status_idx"),
        ]


class AppOrderItem(models.Model):
    order = models.ForeignKey(AppOrder, related_name="items", on_delete=models.CASCADE)
    field = models.ForeignKey(AppDataField, related_name="order_items", on_delete=models.PROTECT)
    seller_user = models.ForeignKey("users.AppUser", related_name="sold_items", on_delete=models.PROTECT)
    match_ref = models.CharField(max_length=80, blank=True, default="")
    kind = models.CharField(max_length=24)
    price_cents = models.PositiveIntegerField(default=0)
    seller_address = models.CharField(max_length=64)
    cdr_vault_uuid = models.CharField(max_length=120, blank=True, null=True)
    access_aux_data = models.TextField(blank=True, null=True)
    license_token_id = models.CharField(max_length=80, blank=True, null=True)
    license_mint_tx_hash = models.CharField(max_length=96, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_order_items"
        constraints = [
            models.UniqueConstraint(fields=["order", "field"], name="app_order_item_unique"),
        ]
        indexes = [
            models.Index(fields=["order"], name="app_item_order_idx"),
            models.Index(fields=["field"], name="app_item_field_idx"),
            models.Index(fields=["seller_address"], name="app_item_seller_idx"),
        ]


class AppOrderSellerPayout(models.Model):
    order = models.ForeignKey(AppOrder, related_name="seller_payouts", on_delete=models.CASCADE)
    seller_address = models.CharField(max_length=64)
    field_ids = models.JSONField(default=list)
    gross_cents = models.PositiveIntegerField(default=0)
    seller_cents = models.PositiveIntegerField(default=0)
    service_fee_cents = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_order_seller_payouts"
        constraints = [
            models.UniqueConstraint(fields=["order", "seller_address"], name="app_order_payout_unique"),
        ]
        indexes = [
            models.Index(fields=["seller_address"], name="app_payout_seller_idx"),
        ]


class AppVerification(models.Model):
    id = models.CharField(max_length=120, primary_key=True)
    field = models.ForeignKey(AppDataField, null=True, blank=True, on_delete=models.CASCADE)
    channel = models.CharField(max_length=16)
    target = models.CharField(max_length=240)
    provider = models.CharField(max_length=40)
    code_hash = models.CharField(max_length=96, blank=True, default="")
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_verifications"
        indexes = [
            models.Index(fields=["field", "-created_at"], name="app_verify_field_idx"),
            models.Index(fields=["target", "-created_at"], name="app_verify_target_idx"),
        ]


class AppExportLog(models.Model):
    order = models.ForeignKey(AppOrder, related_name="export_logs", on_delete=models.CASCADE)
    generated_at = models.DateTimeField()
    format = models.CharField(max_length=16)
    successful_field_ids = models.JSONField(default=list)
    failed_field_ids = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_export_logs"
        indexes = [
            models.Index(fields=["order", "-created_at"], name="app_export_order_idx"),
        ]


class AppExportLogItem(models.Model):
    export_log = models.ForeignKey(AppExportLog, related_name="items", on_delete=models.CASCADE)
    order_item = models.ForeignKey(AppOrderItem, on_delete=models.CASCADE)
    success = models.BooleanField()
    error = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "app_export_log_items"
        constraints = [
            models.UniqueConstraint(fields=["export_log", "order_item"], name="app_export_item_unique"),
        ]

