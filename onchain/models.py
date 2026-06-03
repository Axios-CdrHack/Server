from django.db import models
from django.utils import timezone


class AppCdrVault(models.Model):
    id = models.CharField(max_length=140, primary_key=True)
    field = models.OneToOneField("data.AppDataField", related_name="cdr_vault", on_delete=models.CASCADE)
    network = models.CharField(max_length=40, default="aeneid")
    cdr_vault_uuid = models.CharField(max_length=120)
    slot_id = models.CharField(max_length=96, blank=True, null=True)
    owner_address = models.CharField(max_length=64, blank=True, null=True)
    write_condition_address = models.CharField(max_length=64, blank=True, null=True)
    read_condition_address = models.CharField(max_length=64, blank=True, null=True)
    write_condition_data = models.TextField(blank=True, default="0x")
    read_condition_data = models.TextField(blank=True, null=True)
    allocate_tx_hash = models.CharField(max_length=96, blank=True, null=True)
    deploy_tx_hash = models.CharField(max_length=96, blank=True, null=True)
    cdr_license_ip_id = models.CharField(max_length=64, blank=True, null=True)
    cdr_license_terms_id = models.CharField(max_length=80, blank=True, null=True)
    platform_wallet = models.CharField(max_length=64, blank=True, null=True)
    ipa_recipient = models.CharField(max_length=64, blank=True, null=True)
    ipa_nft_contract = models.CharField(max_length=64, blank=True, null=True)
    ipa_token_id = models.CharField(max_length=80, blank=True, null=True)
    ip_registration_tx_hash = models.CharField(max_length=96, blank=True, null=True)
    ipa_transfer_tx_hash = models.CharField(max_length=96, blank=True, null=True)
    license_config_tx_hash = models.CharField(max_length=96, blank=True, null=True)
    license_attach_tx_hash = models.CharField(max_length=96, blank=True, null=True)
    status = models.CharField(max_length=24, default="active")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_cdr_vaults"
        indexes = [
            models.Index(fields=["field"], name="app_cdr_field_idx"),
            models.Index(fields=["network", "cdr_vault_uuid"], name="app_cdr_network_idx"),
            models.Index(fields=["status"], name="app_cdr_status_idx"),
        ]


class AppOnchainSale(models.Model):
    id = models.CharField(max_length=160, primary_key=True)
    order = models.ForeignKey("data.AppOrder", null=True, blank=True, on_delete=models.SET_NULL)
    field = models.ForeignKey("data.AppDataField", null=True, blank=True, on_delete=models.SET_NULL)
    buyer_wallet = models.CharField(max_length=64)
    seller_address = models.CharField(max_length=64)
    kind = models.CharField(max_length=24, blank=True, default="")
    label = models.CharField(max_length=120, blank=True, default="Paid data")
    cdr_license_ip_id = models.CharField(max_length=64, blank=True, null=True)
    gross_cents = models.PositiveIntegerField(default=0)
    seller_cents = models.PositiveIntegerField(default=0)
    service_fee_cents = models.PositiveIntegerField(default=0)
    payment_tx_hash = models.CharField(max_length=96)
    block_number = models.CharField(max_length=80, blank=True, null=True)
    log_index = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=24, default="onchain")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_onchain_sales"
        constraints = [
            models.UniqueConstraint(fields=["payment_tx_hash", "field"], name="app_onchain_sale_unique"),
        ]
        indexes = [
            models.Index(fields=["seller_address", "-created_at"], name="app_sale_seller_idx"),
            models.Index(fields=["buyer_wallet", "-created_at"], name="app_sale_buyer_idx"),
        ]

