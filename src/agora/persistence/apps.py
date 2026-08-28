from django.apps import AppConfig


class PersistenceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "agora.persistence"

    def ready(self) -> None:
        from agora.db.table_names import configure_django_runtime_table_names
        from agora.persistence import checks  # noqa: F401

        configure_django_runtime_table_names(prefixed=True)
