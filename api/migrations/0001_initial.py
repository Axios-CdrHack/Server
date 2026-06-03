from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="StoreRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("namespace", models.CharField(max_length=64)),
                ("key", models.CharField(max_length=160)),
                ("value", models.JSONField()),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="storerecord",
            constraint=models.UniqueConstraint(fields=("namespace", "key"), name="api_store_namespace_key_unique"),
        ),
        migrations.AddIndex(
            model_name="storerecord",
            index=models.Index(fields=["namespace"], name="api_storere_namespace_4ae725_idx"),
        ),
    ]
