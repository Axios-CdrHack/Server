from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0001_initial"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="storerecord",
            new_name="api_store_ns_idx",
            old_name="api_storere_namespace_4ae725_idx",
        ),
    ]
