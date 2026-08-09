from django.db import models
from django.utils import timezone


class AppUser(models.Model):
    id = models.CharField(max_length=80, primary_key=True)
    privy_user_id = models.CharField(max_length=160, blank=True, null=True)
    email = models.EmailField(blank=True, default="")
    wallet_address = models.CharField(max_length=64, blank=True, null=True)
    smart_wallet_address = models.CharField(max_length=64, blank=True, null=True)
    name = models.CharField(max_length=160, blank=True, default="")
    age = models.PositiveSmallIntegerField(default=0)
    occupation = models.CharField(max_length=160, blank=True, default="")
    gender = models.CharField(max_length=40, blank=True, default="")
    country = models.CharField(max_length=80, blank=True, default="Korea")
    residence = models.CharField(max_length=120, blank=True, default="")
    display_name = models.CharField(max_length=160, blank=True, default="")
    public_slug = models.CharField(max_length=160, blank=True, default="")
    avatar_url = models.TextField(blank=True, null=True)
    payout_address = models.CharField(max_length=64, blank=True, null=True)
    has_profile = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_users"
        indexes = [
            models.Index(fields=["email"], name="app_users_email_idx"),
            models.Index(fields=["wallet_address"], name="app_users_wallet_idx"),
            models.Index(fields=["public_slug"], name="app_users_slug_idx"),
        ]


class WalletAuthChallenge(models.Model):
    nonce = models.CharField(max_length=64, primary_key=True)
    wallet_address = models.CharField(max_length=64, blank=True, default="")
    chain_id = models.PositiveBigIntegerField(default=0)
    domain = models.CharField(max_length=255, blank=True, default="")
    uri = models.TextField(blank=True, default="")
    message = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "wallet_auth_challenges"


class AppEducation(models.Model):
    id = models.CharField(max_length=120, primary_key=True)
    user = models.ForeignKey(AppUser, related_name="educations", on_delete=models.CASCADE)
    education = models.CharField(max_length=240, blank=True, default="", db_column="school")
    status = models.CharField(max_length=40, blank=True, default="graduated")
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_educations"
        indexes = [
            models.Index(fields=["user", "sort_order"], name="app_edu_user_sort_idx"),
        ]


class AppCareer(models.Model):
    id = models.CharField(max_length=120, primary_key=True)
    user = models.ForeignKey(AppUser, related_name="careers", on_delete=models.CASCADE)
    career = models.CharField(max_length=240, blank=True, default="", db_column="title")
    start_date = models.CharField(max_length=16, blank=True, default="")
    end_date = models.CharField(max_length=16, blank=True, default="")
    status = models.CharField(max_length=40, blank=True, default="employed")
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "app_careers"
        indexes = [
            models.Index(fields=["user", "sort_order"], name="app_career_user_sort_idx"),
        ]
