from __future__ import annotations

from pathlib import Path

import pytest
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from agora.config import (
    RuntimeConfig,
    ServiceName,
    load_portal_static_root,
    load_service_secret,
    validate_treasury_package,
)
from agora.core.checks import _overlaps, private_artifact_root_check
from agora.settings.portal import _portal_template_loaders


def valid_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "AGORA_ENVIRONMENT": "test",
        "AGORA_DEBUG": "false",
        "AGORA_PORTAL_SECRET_KEY": "p" * 64,
        "AGORA_CONTENT_SECRET_KEY": "c" * 64,
        "AGORA_PORTAL_ORIGIN": "http://portal.agora.test:8000",
        "AGORA_CONTENT_ORIGIN": "http://content.agorausercontent.test:8001",
        "ENV": "prod",
        "TA_PROD_PASSWORD": "not-a-real-password",
        "AGORA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
    }


def test_portal_template_cache_is_disabled_only_for_development_refresh() -> None:
    sources = [
        "django.template.loaders.filesystem.Loader",
        "django.template.loaders.app_directories.Loader",
    ]

    assert _portal_template_loaders(cache=False) == sources
    assert _portal_template_loaders(cache=True) == [
        ("django.template.loaders.cached.Loader", sources)
    ]


def test_configuration_accepts_explicit_safe_values(tmp_path: Path) -> None:
    environ = valid_environment(tmp_path)
    config = RuntimeConfig.from_environ(environ)

    assert config.environment == "test"
    assert config.debug is False
    assert config.portal_origin.hostname == "portal.agora.test"
    assert config.content_origin.hostname == "content.agorausercontent.test"
    assert config.database.environment == "PROD"
    assert config.database.password_variable == "TA_PROD_PASSWORD"
    assert config.artifact_root.is_absolute()
    assert load_service_secret(environ, "portal") == "p" * 64
    assert load_service_secret(environ, "content") == "c" * 64


def test_configuration_reports_every_missing_required_name() -> None:
    with pytest.raises(ImproperlyConfigured) as raised:
        RuntimeConfig.from_environ({})

    message = str(raised.value)
    for name in (
        "AGORA_ENVIRONMENT",
        "AGORA_DEBUG",
        "AGORA_PORTAL_ORIGIN",
        "AGORA_CONTENT_ORIGIN",
        "ENV",
        "AGORA_ARTIFACT_ROOT",
    ):
        assert name in message


def test_configuration_requires_the_selected_profile_password(tmp_path: Path) -> None:
    environ = valid_environment(tmp_path)
    environ["ENV"] = "SDLC"

    with pytest.raises(ImproperlyConfigured, match="TA_SDLC_PASSWORD"):
        RuntimeConfig.from_environ(environ)

    environ["TA_SDLC_PASSWORD"] = "managed-secret"
    assert RuntimeConfig.from_environ(environ).database.password_variable == "TA_SDLC_PASSWORD"


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("AGORA_DEBUG", "sometimes", "must be true or false"),
        ("ENV", "9prod", "must begin with a letter"),
        ("ENV", "prod profile", "must begin with a letter"),
        ("AGORA_ARTIFACT_ROOT", "relative/path", "must be an absolute path"),
    ],
)
def test_configuration_rejects_invalid_values(
    tmp_path: Path, name: str, value: str, expected: str
) -> None:
    environ = valid_environment(tmp_path)
    environ[name] = value

    with pytest.raises(ImproperlyConfigured, match=expected):
        RuntimeConfig.from_environ(environ)


@pytest.mark.parametrize("service", ["portal", "content"])
def test_service_secret_is_required_and_validated(service: ServiceName) -> None:
    with pytest.raises(ImproperlyConfigured, match="is required"):
        load_service_secret({}, service)

    secret_name = "AGORA_PORTAL_SECRET_KEY" if service == "portal" else "AGORA_CONTENT_SECRET_KEY"
    with pytest.raises(ImproperlyConfigured, match="at least 50 characters"):
        load_service_secret({secret_name: "short"}, service)
    with pytest.raises(ImproperlyConfigured, match="placeholder"):
        load_service_secret({secret_name: "GENERATE_WITH_BOOTSTRAP"}, service)


@pytest.mark.parametrize(
    "origin",
    [
        "portal.agora.test",
        "ftp://portal.agora.test",
        "http://user@portal.agora.test",
        "http://portal.agora.test/path",
        "http://portal.agora.test/",
        "http://portal.agora.test?query=yes",
        "http://portal.agora.test#fragment",
        "http://portal.agora.test:invalid",
        "http://portal.agora.test:08000",
    ],
)
def test_configuration_rejects_non_origins(tmp_path: Path, origin: str) -> None:
    environ = valid_environment(tmp_path)
    environ["AGORA_PORTAL_ORIGIN"] = origin

    with pytest.raises(ImproperlyConfigured, match="AGORA_PORTAL_ORIGIN"):
        RuntimeConfig.from_environ(environ)


def test_configuration_rejects_same_hostname_even_on_different_ports(tmp_path: Path) -> None:
    environ = valid_environment(tmp_path)
    environ["AGORA_CONTENT_ORIGIN"] = "http://portal.agora.test:8001"

    with pytest.raises(ImproperlyConfigured, match="different hostnames"):
        RuntimeConfig.from_environ(environ)


def test_configuration_rejects_shared_service_secret(tmp_path: Path) -> None:
    environ = valid_environment(tmp_path)
    environ["AGORA_CONTENT_SECRET_KEY"] = environ["AGORA_PORTAL_SECRET_KEY"]

    with pytest.raises(ImproperlyConfigured, match="secret keys must be different"):
        load_service_secret(environ, "content")


def test_production_requires_https_and_disables_debug(tmp_path: Path) -> None:
    environ = valid_environment(tmp_path)
    environ["AGORA_ENVIRONMENT"] = "production"
    environ["AGORA_DEBUG"] = "true"

    with pytest.raises(ImproperlyConfigured) as raised:
        RuntimeConfig.from_environ(environ)

    message = str(raised.value)
    assert "AGORA_DEBUG must be false" in message
    assert "AGORA_PORTAL_ORIGIN must use https" in message
    assert "AGORA_CONTENT_ORIGIN must use https" in message


def test_valid_production_configuration_is_explicit_and_secure(tmp_path: Path) -> None:
    environ = valid_environment(tmp_path)
    environ.update(
        {
            "AGORA_ENVIRONMENT": "production",
            "AGORA_PORTAL_ORIGIN": "https://portal.example.test",
            "AGORA_CONTENT_ORIGIN": "https://content.exampleusercontent.test",
        }
    )

    config = RuntimeConfig.from_environ(environ)

    assert config.is_production is True
    assert config.portal_origin.scheme == "https"
    assert config.content_origin.scheme == "https"


def test_production_rejects_the_repository_treasury_stand_in() -> None:
    validate_treasury_package(
        "development",
        development_stand_in=True,
        distribution_present=True,
        distribution_summary="Local stand-in for managed Treasury Analytics",
    )
    validate_treasury_package(
        "test",
        development_stand_in=True,
        distribution_present=True,
        distribution_summary="Local stand-in for managed Treasury Analytics",
    )
    validate_treasury_package(
        "production",
        development_stand_in=False,
        distribution_present=True,
        distribution_summary="Managed Treasury Analytics",
    )

    with pytest.raises(ImproperlyConfigured, match="managed treasury-analytics package"):
        validate_treasury_package(
            "production",
            development_stand_in=True,
            distribution_present=True,
            distribution_summary="Local stand-in for managed Treasury Analytics",
        )


@pytest.mark.parametrize(
    ("development_stand_in", "distribution_present", "distribution_summary"),
    [
        (False, True, "Local stand-in for Treasury Analytics"),
        (False, True, "Development-only Treasury Analytics implementation"),
        (False, False, ""),
    ],
)
def test_production_rejects_stale_or_missing_treasury_distributions(
    development_stand_in: bool,
    distribution_present: bool,
    distribution_summary: str,
) -> None:
    with pytest.raises(ImproperlyConfigured, match="managed treasury-analytics package"):
        validate_treasury_package(
            "production",
            development_stand_in=development_stand_in,
            distribution_present=distribution_present,
            distribution_summary=distribution_summary,
        )


def test_production_requires_an_explicit_absolute_static_root(tmp_path: Path) -> None:
    with pytest.raises(ImproperlyConfigured, match="AGORA_STATIC_ROOT is required"):
        load_portal_static_root(
            {},
            environment="production",
            development_default=tmp_path / "local-static",
        )

    with pytest.raises(ImproperlyConfigured, match="must be an absolute path"):
        load_portal_static_root(
            {"AGORA_STATIC_ROOT": "relative/static"},
            environment="production",
            development_default=tmp_path / "local-static",
        )

    production_root = tmp_path / "production-static"
    assert (
        load_portal_static_root(
            {"AGORA_STATIC_ROOT": str(production_root)},
            environment="production",
            development_default=tmp_path / "local-static",
        )
        == production_root.resolve()
    )


def test_nonproduction_static_root_keeps_the_local_default(tmp_path: Path) -> None:
    local_root = tmp_path / "local-static"

    assert (
        load_portal_static_root(
            {},
            environment="test",
            development_default=local_root,
        )
        == local_root.resolve()
    )


def test_configuration_rejects_unknown_environment(tmp_path: Path) -> None:
    environ = valid_environment(tmp_path)
    environ["AGORA_ENVIRONMENT"] = "staging"

    with pytest.raises(ImproperlyConfigured, match="development, test, or production"):
        RuntimeConfig.from_environ(environ)


def test_configuration_rejects_origin_whitespace(tmp_path: Path) -> None:
    environ = valid_environment(tmp_path)
    environ["AGORA_CONTENT_ORIGIN"] = "http://content.agorausercontent.test:8001 newline"

    with pytest.raises(ImproperlyConfigured, match="must not contain whitespace"):
        RuntimeConfig.from_environ(environ)


def test_error_never_echoes_secret_values(tmp_path: Path) -> None:
    environ = valid_environment(tmp_path)
    portal_secret = environ["AGORA_PORTAL_SECRET_KEY"]
    content_secret = environ["AGORA_CONTENT_SECRET_KEY"]
    password = environ["TA_PROD_PASSWORD"]
    environ["ENV"] = "invalid profile"

    with pytest.raises(ImproperlyConfigured) as raised:
        RuntimeConfig.from_environ(environ)

    message = str(raised.value)
    assert portal_secret not in message
    assert content_secret not in message
    assert password not in message

    with pytest.raises(ImproperlyConfigured) as secret_error:
        load_service_secret(
            {
                "AGORA_PORTAL_SECRET_KEY": "short",
                "AGORA_CONTENT_SECRET_KEY": content_secret,
            },
            "portal",
        )
    assert "short" not in str(secret_error.value)
    assert content_secret not in str(secret_error.value)


def test_private_artifact_root_is_disjoint_from_served_roots(tmp_path: Path) -> None:
    with override_settings(
        AGORA_ARTIFACT_ROOT=tmp_path / "artifacts",
        STATIC_ROOT=tmp_path / "static",
        MEDIA_ROOT=tmp_path / "media",
        STATICFILES_DIRS=(tmp_path / "assets",),
    ):
        assert private_artifact_root_check(None) == []


@pytest.mark.parametrize(
    ("artifact", "public"),
    [
        ("public", "public"),
        ("public/artifacts", "public"),
        ("private", "private/static"),
    ],
)
def test_private_artifact_root_rejects_every_overlap_form(
    tmp_path: Path, artifact: str, public: str
) -> None:
    artifact_root = tmp_path / artifact
    public_root = tmp_path / public
    assert _overlaps(artifact_root, public_root)
    with override_settings(
        AGORA_ARTIFACT_ROOT=artifact_root,
        STATIC_ROOT=public_root,
        MEDIA_ROOT="",
        STATICFILES_DIRS=(),
    ):
        errors = private_artifact_root_check(None)

    assert [error.id for error in errors] == ["agora.E001"]


def test_private_artifact_root_rejects_app_discovered_static_directory() -> None:
    portal_static = Path(apps.get_app_config("portal").path) / "static"
    assert portal_static.is_dir()
    with override_settings(
        AGORA_ARTIFACT_ROOT=portal_static / "private-artifacts",
        STATIC_ROOT="",
        MEDIA_ROOT="",
        STATICFILES_DIRS=(),
    ):
        errors = private_artifact_root_check(None)

    assert [error.id for error in errors] == ["agora.E001"]
