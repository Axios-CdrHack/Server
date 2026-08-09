from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalletAuthChallenge",
            fields=[
                ("nonce", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("wallet_address", models.CharField(blank=True, default="", max_length=64)),
                ("chain_id", models.PositiveBigIntegerField(default=0)),
                ("domain", models.CharField(blank=True, default="", max_length=255)),
                ("uri", models.TextField(blank=True, default="")),
                ("message", models.TextField(blank=True, default="")),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"db_table": "wallet_auth_challenges"},
        ),
    ]
