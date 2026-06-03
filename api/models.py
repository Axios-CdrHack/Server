from django.db import models


class StoreRecord(models.Model):
    namespace = models.CharField(max_length=64)
    key = models.CharField(max_length=160)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["namespace", "key"], name="api_store_namespace_key_unique"),
        ]
        indexes = [
            models.Index(fields=["namespace"], name="api_store_ns_idx"),
        ]
