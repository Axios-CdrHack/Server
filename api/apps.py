from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _configure_sqlite(connection, **_kwargs):
    """Enable WAL so concurrent writers (e.g. the 5 parallel POST /fields a single
    save fires) queue on the busy_timeout instead of deadlocking on the read->write
    lock upgrade that rollback-journal mode hits ('database is locked')."""
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=20000;")


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    def ready(self):
        connection_created.connect(_configure_sqlite)
