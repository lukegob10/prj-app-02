from django.apps import AppConfig


class PersistenceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "agora.persistence"

    def ready(self) -> None:
        from agora.persistence import checks  # noqa: F401
