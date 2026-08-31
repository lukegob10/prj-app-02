from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    # The Python package was renamed, but ``persistence`` is the stable Django
    # application identity used by migrations, content types, permissions, and
    # existing database metadata.
    name = "agora.core"
    label = "persistence"

    def ready(self) -> None:
        from agora.core import checks  # noqa: F401
        from agora.db.table_names import configure_django_runtime_table_names

        configure_django_runtime_table_names(prefixed=True)
