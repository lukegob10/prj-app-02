from __future__ import annotations

import io
import secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NoReturn
from uuid import UUID

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.test import Client, RequestFactory, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import agora.persistence.authentication as identity
from agora.persistence.authentication import (
    BootstrapAlreadyComplete,
    DuplicateSoeid,
    LastAdministratorError,
    NotAdministrator,
    PasswordPolicyError,
    SelfDisableError,
    UserNotFound,
    authenticate_login,
    bootstrap_first_administrator,
    disable_user,
    enable_user,
    provision_user,
    reset_user_password,
)
from agora.persistence.models import AuditEvent, LoginThrottle, User
from agora.portal.forms import ProvisionUserForm, ResetPasswordForm
from agora.portal.security import safe_next_url

pytestmark = pytest.mark.django_db(transaction=True)

PORTAL_HOST = "portal.agora.test:8000"
LOCAL_HTTPS_HOST = "localhost:8443"
LOCAL_HTTPS_ORIGIN = f"https://{LOCAL_HTTPS_HOST}"


def strong_password() -> str:
    return secrets.token_urlsafe(32)


@pytest.fixture
def admin_identity() -> tuple[User, str]:
    password = strong_password()
    return bootstrap_first_administrator(" admin.1 ", password), password


@pytest.fixture
def regular_identity(admin_identity: tuple[User, str]) -> tuple[User, str]:
    admin, _ = admin_identity
    password = strong_password()
    user = provision_user(actor_id=admin.id, soeid="person.1", password=password)
    return user, password


def login_client(client: Client, soeid: str, password: str, **extra: str) -> Any:
    data: dict[str, str] = {"soeid": soeid, "password": password, "next": ""}
    data.update(extra)
    return client.post(reverse("login"), data, HTTP_HOST=PORTAL_HOST)


def test_bootstrap_hashes_credentials_and_is_one_time() -> None:
    password = strong_password()
    user = bootstrap_first_administrator("\tfirst.admin\n", password)

    assert user.soeid == "FIRST.ADMIN"
    assert user.is_administrator is True
    assert user.has_usable_password()
    assert user.password != password
    assert user.check_password(password)
    event = AuditEvent.objects.get()
    assert event.event_type == "auth.bootstrap"
    assert event.actor_id is None
    assert event.target_user_id == user.id
    assert event.metadata == {}

    with pytest.raises(BootstrapAlreadyComplete):
        bootstrap_first_administrator("SECOND.ADMIN", strong_password())
    assert User.objects.count() == 1
    assert AuditEvent.objects.count() == 1


@pytest.mark.parametrize("password", ["short", "123456789012", "first.admin123"])
def test_bootstrap_rejects_weak_or_similar_password_without_persisting(password: str) -> None:
    with pytest.raises(PasswordPolicyError):
        bootstrap_first_administrator("FIRST.ADMIN", password)

    assert User.objects.count() == 0
    assert AuditEvent.objects.count() == 0


def test_bootstrap_command_uses_hidden_input_and_redacts_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = strong_password()
    prompts: list[str] = []

    def hidden_prompt(prompt: str) -> str:
        prompts.append(prompt)
        return password

    monkeypatch.setattr(
        "agora.persistence.management.commands.bootstrap_admin.getpass.getpass",
        hidden_prompt,
    )
    output = io.StringIO()
    call_command("bootstrap_admin", soeid="COMMAND.ADMIN", stdout=output)

    assert prompts == [
        "First administrator password: ",
        "Confirm first administrator password: ",
    ]
    assert output.getvalue() == "First administrator created.\n"
    assert password not in output.getvalue()
    assert "COMMAND.ADMIN" not in output.getvalue()

    def unexpected_prompt(prompt: str) -> str:
        raise AssertionError(prompt)

    monkeypatch.setattr(
        "agora.persistence.management.commands.bootstrap_admin.getpass.getpass",
        unexpected_prompt,
    )
    with pytest.raises(CommandError, match="before any users"):
        call_command("bootstrap_admin", soeid="SECOND.ADMIN")


def test_bootstrap_command_rejects_mismatch_without_creating_a_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = strong_password()
    values = iter((password, strong_password()))
    monkeypatch.setattr(
        "agora.persistence.management.commands.bootstrap_admin.getpass.getpass",
        lambda prompt: next(values),
    )

    with pytest.raises(CommandError, match="do not match"):
        call_command("bootstrap_admin", soeid="COMMAND.ADMIN")
    assert User.objects.count() == 0


def test_bootstrap_command_maps_invalid_soeid_and_password_policy_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = strong_password()
    values = iter((password, password))
    monkeypatch.setattr(
        "agora.persistence.management.commands.bootstrap_admin.getpass.getpass",
        lambda prompt: next(values),
    )
    with pytest.raises(CommandError, match="SOEID is not valid"):
        call_command("bootstrap_admin", soeid="!!!")
    assert User.objects.count() == 0

    values = iter(("short", "short"))
    monkeypatch.setattr(
        "agora.persistence.management.commands.bootstrap_admin.getpass.getpass",
        lambda prompt: next(values),
    )
    with pytest.raises(CommandError, match="password does not meet"):
        call_command("bootstrap_admin", soeid="VALID.ADMIN")
    assert User.objects.count() == 0

    def race(*args: object, **kwargs: object) -> NoReturn:
        raise BootstrapAlreadyComplete

    monkeypatch.setattr(
        "agora.persistence.management.commands.bootstrap_admin.bootstrap_first_administrator",
        race,
    )
    password = strong_password()
    values = iter((password, password))
    monkeypatch.setattr(
        "agora.persistence.management.commands.bootstrap_admin.getpass.getpass",
        lambda prompt: next(values),
    )
    with pytest.raises(CommandError, match="before any users"):
        call_command("bootstrap_admin", soeid="RACE.ADMIN")
    assert User.objects.count() == 0


def test_provisioning_requires_an_administrator_and_rejects_canonical_duplicates(
    admin_identity: tuple[User, str],
) -> None:
    admin, _ = admin_identity
    non_admin = User.objects.create_user("PERSON.2")
    with pytest.raises(NotAdministrator):
        provision_user(
            actor_id=non_admin.id,
            soeid="NEW.USER",
            password=strong_password(),
        )

    created = provision_user(
        actor_id=admin.id,
        soeid=" new.user ",
        password=strong_password(),
        is_administrator=True,
    )
    assert created.soeid == "NEW.USER"
    assert created.is_administrator is True
    assert created.check_password(created.password) is False

    with pytest.raises(DuplicateSoeid):
        provision_user(
            actor_id=admin.id,
            soeid="\tnew.user\n",
            password=strong_password(),
        )
    assert User.objects.filter(soeid="NEW.USER").count() == 1
    assert [event.event_type for event in AuditEvent.objects.order_by("id")] == [
        "auth.bootstrap",
        "user.created",
    ]


def test_password_reset_disable_and_enable_are_atomic_and_audited(
    admin_identity: tuple[User, str],
) -> None:
    admin, _ = admin_identity
    second_admin = provision_user(
        actor_id=admin.id,
        soeid="SECOND.ADMIN",
        password=strong_password(),
        is_administrator=True,
    )
    target_password = strong_password()
    target = provision_user(actor_id=admin.id, soeid="TARGET.USER", password=target_password)
    original_hash = target.password
    original_version = target.auth_version

    disabled = disable_user(actor_id=admin.id, target_id=target.id)
    assert disabled.is_active is False
    assert disabled.auth_version == original_version + 1
    assert AuditEvent.objects.filter(event_type="user.disabled").count() == 1

    enabled = enable_user(actor_id=admin.id, target_id=target.id)
    assert enabled.is_active is True
    assert enabled.auth_version == original_version + 2
    assert AuditEvent.objects.filter(event_type="user.enabled").count() == 1

    replacement_password = strong_password()
    reset = reset_user_password(
        actor_id=second_admin.id,
        target_id=target.id,
        password=replacement_password,
    )
    assert reset.password != original_hash
    assert reset.auth_version == original_version + 3
    assert reset.check_password(replacement_password)
    assert not reset.check_password(target_password)
    assert AuditEvent.objects.filter(event_type="user.password_reset").count() == 1
    assert all(event.metadata == {} for event in AuditEvent.objects.all())


def test_last_active_administrator_and_self_disable_are_protected(
    admin_identity: tuple[User, str],
) -> None:
    admin, _ = admin_identity
    with pytest.raises(LastAdministratorError):
        disable_user(actor_id=admin.id, target_id=admin.id)

    second_admin = provision_user(
        actor_id=admin.id,
        soeid="SECOND.ADMIN",
        password=strong_password(),
        is_administrator=True,
    )
    with pytest.raises(SelfDisableError):
        disable_user(actor_id=admin.id, target_id=admin.id)
    disable_user(actor_id=second_admin.id, target_id=admin.id)
    with pytest.raises(LastAdministratorError):
        disable_user(actor_id=second_admin.id, target_id=second_admin.id)
    assert User.objects.filter(is_active=True, is_administrator=True).count() == 1


def test_concurrent_last_administrator_operations_leave_one_active() -> None:
    first = bootstrap_first_administrator("FIRST.ADMIN", strong_password())
    second = provision_user(
        actor_id=first.id,
        soeid="SECOND.ADMIN",
        password=strong_password(),
        is_administrator=True,
    )

    def attempt(actor_id: UUID, target_id: UUID) -> str:
        close_old_connections()
        try:
            disable_user(actor_id=actor_id, target_id=target_id)
        except NotAdministrator:
            return "not-administrator"
        except LastAdministratorError:
            return "last-administrator"
        finally:
            close_old_connections()
        return "disabled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda values: attempt(*values),
                ((first.id, second.id), (second.id, first.id)),
            )
        )

    assert results.count("disabled") == 1
    assert sum(result in {"not-administrator", "last-administrator"} for result in results) == 1
    assert User.objects.filter(is_active=True, is_administrator=True).count() == 1


def test_authentication_normalizes_form_identity_and_ignores_identity_headers(
    admin_identity: tuple[User, str],
) -> None:
    admin, password = admin_identity
    factory = RequestFactory()
    request = factory.post(
        reverse("login"),
        HTTP_REMOTE_USER="IMPOSTOR",
        HTTP_X_SOEID="IMPOSTOR",
        REMOTE_ADDR="192.0.2.10",
    )

    authenticated = authenticate_login(request, "\t admin.1 \n", password)
    assert authenticated is not None
    assert authenticated.id == admin.id

    forged = authenticate_login(request, "IMPOSTOR", password)
    assert forged is None
    assert LoginThrottle.objects.count() >= 1
    assert all("IMPOSTOR" not in bucket.bucket_hash for bucket in LoginThrottle.objects.all())


def test_invalid_disabled_and_wrong_credentials_have_the_same_generic_web_response(
    admin_identity: tuple[User, str],
    regular_identity: tuple[User, str],
) -> None:
    admin, _ = admin_identity
    target, target_password = regular_identity
    disable_user(actor_id=admin.id, target_id=target.id)

    wrong_password = strong_password()
    cases = (
        {"soeid": "ADMIN.1", "password": wrong_password},
        {"soeid": "NOT.PROVISIONED", "password": wrong_password},
        {"soeid": "PERSON.1", "password": target_password},
        {"soeid": "!!!", "password": wrong_password},
    )
    responses = [
        Client().post(
            reverse("login"),
            {**case, "next": ""},
            HTTP_HOST=PORTAL_HOST,
            REMOTE_ADDR=f"192.0.2.{index + 20}",
        )
        for index, case in enumerate(cases)
    ]
    assert all(response.status_code == 200 for response in responses)
    assert all(
        b"Sign-in failed. Check your SOEID and password." in response.content
        for response in responses
    )
    assert all(wrong_password.encode() not in response.content for response in responses)
    assert b"TARGET.USER" not in responses[-1].content


def test_successful_login_rotates_session_and_uses_safe_next_redirect(
    admin_identity: tuple[User, str],
) -> None:
    admin, password = admin_identity
    client = Client()
    session = client.session
    session["anonymous_marker"] = "retained"
    session.save()
    old_session_key = session.session_key

    response = login_client(client, "\tadmin.1\n", password, next="/admin/users/")

    assert response.status_code == 302
    assert response["Location"] == "/admin/users/"
    assert client.session.session_key != old_session_key
    assert client.session["anonymous_marker"] == "retained"
    assert client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).status_code == 200
    assert b"Welcome back, ADMIN.1" in client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content
    assert AuditEvent.objects.filter(event_type="auth.login.succeeded", actor_id=admin.id).exists()


@pytest.mark.parametrize(
    "candidate",
    [
        "https://evil.example/steal",
        "//evil.example/steal",
        "http://content.agorausercontent.test:8001/",
        "/\\\\evil.example",
        "/safe\r\nLocation: https://evil.example",
    ],
)
def test_next_url_accepts_only_relative_portal_paths(candidate: str) -> None:
    request = RequestFactory().get(reverse("login"), HTTP_HOST=PORTAL_HOST)
    assert safe_next_url(request, candidate) == "/"

    safe = "/admin/users/?tab=active"
    assert safe_next_url(request, safe) == safe


def test_unsafe_next_is_not_reflected_or_used_after_login(
    admin_identity: tuple[User, str],
) -> None:
    _, password = admin_identity
    client = Client()
    response = client.get(
        f"{reverse('login')}?next=https%3A%2F%2Fevil.example%2Fsteal",
        HTTP_HOST=PORTAL_HOST,
    )
    assert b"evil.example" not in response.content

    response = login_client(client, "ADMIN.1", password, next="https://evil.example/steal")
    assert response["Location"] == "/"


def test_post_logout_flushes_session_and_get_logout_is_non_mutating(
    admin_identity: tuple[User, str],
) -> None:
    admin, password = admin_identity
    client = Client()
    login_client(client, "ADMIN.1", password)
    authenticated = client.get(reverse("home"), HTTP_HOST=PORTAL_HOST)
    assert b"Welcome back" in authenticated.content

    get_response = client.get(reverse("logout"), HTTP_HOST=PORTAL_HOST)
    assert get_response.status_code == 405
    assert b"Welcome back" in client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content

    post_response = client.post(reverse("logout"), HTTP_HOST=PORTAL_HOST)
    assert post_response.status_code == 302
    assert post_response["Location"] == "/login/"
    assert b"Welcome back" not in client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content
    assert AuditEvent.objects.filter(event_type="auth.logout", actor_id=admin.id).exists()


def test_secure_host_only_cookie_settings_are_emitted_on_portal_login(
    admin_identity: tuple[User, str],
) -> None:
    _, password = admin_identity
    client = Client()
    login_page = client.get(reverse("login"), HTTP_HOST=PORTAL_HOST)
    csrf_cookie = login_page.cookies["__Host-agora_csrf"]
    assert csrf_cookie["secure"] is True
    assert csrf_cookie["httponly"] is True
    assert csrf_cookie["samesite"] == "Lax"
    assert csrf_cookie["path"] == "/"
    assert csrf_cookie["domain"] == ""

    response = login_client(client, "ADMIN.1", password)
    session_cookie = response.cookies["__Host-agora_session"]
    assert session_cookie["secure"] is True
    assert session_cookie["httponly"] is True
    assert session_cookie["samesite"] == "Lax"
    assert session_cookie["path"] == "/"
    assert session_cookie["domain"] == ""


def csrf_token(client: Client) -> str:
    client.get(reverse("login"), HTTP_HOST=PORTAL_HOST)
    return client.cookies["__Host-agora_csrf"].value


def test_csrf_protects_login_logout_and_admin_mutations(
    admin_identity: tuple[User, str],
) -> None:
    admin, password = admin_identity
    client = Client(enforce_csrf_checks=True)
    token = csrf_token(client)

    missing = client.post(
        reverse("login"),
        {"soeid": "ADMIN.1", "password": password},
        HTTP_HOST=PORTAL_HOST,
    )
    assert missing.status_code == 403

    cross_origin = client.post(
        reverse("login"),
        {"soeid": "ADMIN.1", "password": password},
        HTTP_HOST=PORTAL_HOST,
        HTTP_X_CSRFTOKEN=token,
        HTTP_ORIGIN="http://content.agorausercontent.test:8001",
    )
    assert cross_origin.status_code == 403

    success = client.post(
        reverse("login"),
        {"soeid": "ADMIN.1", "password": password},
        HTTP_HOST=PORTAL_HOST,
        HTTP_X_CSRFTOKEN=token,
    )
    assert success.status_code == 302

    logout_missing = client.post(reverse("logout"), HTTP_HOST=PORTAL_HOST)
    assert logout_missing.status_code == 403
    admin_page = client.get(reverse("admin-user-create"), HTTP_HOST=PORTAL_HOST)
    assert admin_page.status_code == 200
    mutation_missing = client.post(
        reverse("admin-user-create"),
        {"soeid": "NEW.USER", "password": strong_password(), "password_confirmation": ""},
        HTTP_HOST=PORTAL_HOST,
    )
    assert mutation_missing.status_code == 403
    assert User.objects.filter(soeid="NEW.USER").exists() is False
    assert User.objects.get(id=admin.id).is_active is True


@override_settings(
    AGORA_ALLOW_OPAQUE_LOOPBACK_ORIGIN=True,
    AGORA_ENVIRONMENT="development",
    AGORA_PORTAL_ORIGIN=LOCAL_HTTPS_ORIGIN,
    ALLOWED_HOSTS=["localhost"],
)
def test_local_https_accepts_opaque_origin_only_with_a_valid_csrf_token(
    admin_identity: tuple[User, str],
) -> None:
    _, password = admin_identity
    client = Client(enforce_csrf_checks=True)
    client.get(
        reverse("login"),
        HTTP_HOST=LOCAL_HTTPS_HOST,
        REMOTE_ADDR="127.0.0.1",
        secure=True,
    )
    token = client.cookies["__Host-agora_csrf"].value

    missing_token = client.post(
        reverse("login"),
        {"soeid": "ADMIN.1", "password": password},
        HTTP_HOST=LOCAL_HTTPS_HOST,
        HTTP_ORIGIN="null",
        REMOTE_ADDR="127.0.0.1",
        secure=True,
    )
    assert missing_token.status_code == 403

    success = client.post(
        reverse("login"),
        {"soeid": "ADMIN.1", "password": password},
        HTTP_HOST=LOCAL_HTTPS_HOST,
        HTTP_ORIGIN="null",
        HTTP_X_CSRFTOKEN=token,
        REMOTE_ADDR="127.0.0.1",
        secure=True,
    )
    assert success.status_code == 302
    assert client.get(reverse("home"), HTTP_HOST=LOCAL_HTTPS_HOST, secure=True).status_code == 200


@pytest.mark.parametrize(
    ("environment", "enabled", "remote_address", "secure"),
    [
        ("production", True, "127.0.0.1", True),
        ("development", False, "127.0.0.1", True),
        ("development", True, "192.0.2.10", True),
        ("development", True, "127.0.0.1", False),
    ],
)
@override_settings(
    AGORA_PORTAL_ORIGIN=LOCAL_HTTPS_ORIGIN,
    ALLOWED_HOSTS=["localhost"],
)
def test_opaque_origin_compatibility_fails_closed_outside_local_https(
    admin_identity: tuple[User, str],
    environment: str,
    enabled: bool,
    remote_address: str,
    secure: bool,
) -> None:
    _, password = admin_identity
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("login"), HTTP_HOST=LOCAL_HTTPS_HOST, secure=True)
    token = client.cookies["__Host-agora_csrf"].value

    with override_settings(
        AGORA_ALLOW_OPAQUE_LOOPBACK_ORIGIN=enabled,
        AGORA_ENVIRONMENT=environment,
    ):
        response = client.post(
            reverse("login"),
            {"soeid": "ADMIN.1", "password": password},
            HTTP_HOST=LOCAL_HTTPS_HOST,
            HTTP_ORIGIN="null",
            HTTP_X_CSRFTOKEN=token,
            REMOTE_ADDR=remote_address,
            secure=secure,
        )

    assert response.status_code == 403


def test_disabled_session_does_not_revive_after_reenable(
    admin_identity: tuple[User, str],
    regular_identity: tuple[User, str],
) -> None:
    admin, admin_password = admin_identity
    target, target_password = regular_identity
    target_client = Client()
    admin_client = Client()
    login_client(target_client, "PERSON.1", target_password)
    login_client(admin_client, "ADMIN.1", admin_password)

    assert (
        b"Welcome back, PERSON.1"
        in target_client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content
    )
    disable_user(actor_id=admin.id, target_id=target.id)
    assert b"Welcome back" not in target_client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content

    enable_user(actor_id=admin.id, target_id=target.id)
    assert b"Welcome back" not in target_client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content
    assert target_client.session.session_key is None

    fresh_login = login_client(target_client, "PERSON.1", target_password)
    assert fresh_login.status_code == 302
    assert (
        b"Welcome back, PERSON.1"
        in target_client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content
    )


def test_password_reset_invalidates_existing_session(
    admin_identity: tuple[User, str],
    regular_identity: tuple[User, str],
) -> None:
    admin, _ = admin_identity
    target, target_password = regular_identity
    target_client = Client()
    login_client(target_client, "PERSON.1", target_password)
    assert b"Welcome back" in target_client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content

    replacement = strong_password()
    reset_user_password(actor_id=admin.id, target_id=target.id, password=replacement)
    assert b"Welcome back" not in target_client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content

    assert login_client(target_client, "PERSON.1", target_password).status_code == 200
    assert login_client(target_client, "PERSON.1", replacement).status_code == 302
    assert (
        b"Welcome back, PERSON.1"
        in target_client.get(reverse("home"), HTTP_HOST=PORTAL_HOST).content
    )


def test_admin_ui_has_privilege_boundary_and_explicit_mutations(
    admin_identity: tuple[User, str],
    regular_identity: tuple[User, str],
) -> None:
    admin, admin_password = admin_identity
    target, target_password = regular_identity
    admin_client = Client()
    regular_client = Client()
    login_client(admin_client, "ADMIN.1", admin_password)
    login_client(regular_client, "PERSON.1", target_password)

    forbidden = regular_client.get(reverse("admin-user-list"), HTTP_HOST=PORTAL_HOST)
    assert forbidden.status_code == 403
    assert b"Provisioned users" not in forbidden.content

    listing = admin_client.get(reverse("admin-user-list"), HTTP_HOST=PORTAL_HOST)
    assert listing.status_code == 200
    assert b'<body class="portal-page portal-page--workspace">' in listing.content
    assert b"ADMIN.1" in listing.content
    assert b"PERSON.1" in listing.content
    assert b"Create user" in listing.content

    new_password = strong_password()
    created = admin_client.post(
        reverse("admin-user-create"),
        {
            "soeid": "created.user",
            "password": new_password,
            "password_confirmation": new_password,
            "is_administrator": "on",
            "untrusted_user_id": str(admin.id),
        },
        HTTP_HOST=PORTAL_HOST,
    )
    assert created.status_code == 302
    assert User.objects.filter(soeid="CREATED.USER", is_administrator=True).exists()
    assert new_password.encode() not in created.content

    confirm = admin_client.get(
        reverse("admin-user-disable", args=[target.id]), HTTP_HOST=PORTAL_HOST
    )
    assert confirm.status_code == 200
    assert b'method="post"' in confirm.content
    assert b"I understand this will end the user" in confirm.content

    not_confirmed = admin_client.post(
        reverse("admin-user-disable", args=[target.id]), HTTP_HOST=PORTAL_HOST
    )
    assert not_confirmed.status_code == 200
    target.refresh_from_db()
    assert target.is_active is True

    disabled = admin_client.post(
        reverse("admin-user-disable", args=[target.id]),
        {"confirm": "on"},
        HTTP_HOST=PORTAL_HOST,
    )
    assert disabled.status_code == 302
    target.refresh_from_db()
    assert not bool(target.is_active)

    enabled = admin_client.post(
        reverse("admin-user-enable", args=[target.id]), HTTP_HOST=PORTAL_HOST
    )
    assert enabled.status_code == 302
    target.refresh_from_db()
    assert target.is_active is True

    replacement = strong_password()
    reset = admin_client.post(
        reverse("admin-user-reset-password", args=[target.id]),
        {"password": replacement, "password_confirmation": replacement},
        HTTP_HOST=PORTAL_HOST,
    )
    assert reset.status_code == 302
    assert replacement.encode() not in reset.content
    target.refresh_from_db()
    assert target.check_password(replacement)


def test_admin_user_list_is_keyset_bounded_and_query_constant(
    admin_identity: tuple[User, str],
) -> None:
    admin, password = admin_identity
    client = Client()
    login_client(client, admin.soeid, password)

    with CaptureQueriesContext(connection) as baseline_queries:
        baseline = client.get(reverse("admin-user-list"), HTTP_HOST=PORTAL_HOST)
    assert baseline.status_code == 200
    assert len(baseline_queries) <= 3

    for number in range(30):
        User.objects.create_user(f"DIRECTORY.USER{number:02d}")

    with CaptureQueriesContext(connection) as populated_queries:
        populated = client.get(reverse("admin-user-list"), HTTP_HOST=PORTAL_HOST)
    assert populated.status_code == 200
    assert len(populated.context["users"]) == 25
    assert populated.context["user_page"].next_url is not None
    assert len(populated_queries) == len(baseline_queries)
    assert len(populated_queries) <= 3
    assert all("COUNT(" not in query["sql"].upper() for query in populated_queries)
    assert all(" OFFSET " not in query["sql"].upper() for query in populated_queries)

    with CaptureQueriesContext(connection) as next_queries:
        next_page = client.get(
            populated.context["user_page"].next_url,
            HTTP_HOST=PORTAL_HOST,
        )
    assert next_page.status_code == 200
    assert len(next_queries) == len(populated_queries)
    assert all("COUNT(" not in query["sql"].upper() for query in next_queries)
    assert all(" OFFSET " not in query["sql"].upper() for query in next_queries)


def test_admin_user_search_is_canonical_prefix_only_and_invalid_input_is_bounded(
    admin_identity: tuple[User, str],
) -> None:
    admin, password = admin_identity
    User.objects.create_user("SEARCH.MATCH.A")
    User.objects.create_user("SEARCH.MATCH.B")
    User.objects.create_user("OTHER.USER")
    client = Client()
    login_client(client, admin.soeid, password)

    with CaptureQueriesContext(connection) as search_queries:
        response = client.get(
            reverse("admin-user-list"),
            {"query": "  search.match  "},
            HTTP_HOST=PORTAL_HOST,
        )
    assert response.status_code == 200
    assert response.context["search_query"] == "SEARCH.MATCH"
    assert b"SEARCH.MATCH.A" in response.content
    assert b"SEARCH.MATCH.B" in response.content
    assert b"OTHER.USER" not in response.content
    assert len(search_queries) <= 3
    assert any("LIKE" in query["sql"].upper() for query in search_queries)

    oversized = "A" * 10_000
    with CaptureQueriesContext(connection) as invalid_queries:
        invalid = client.get(
            reverse("admin-user-list"),
            {"query": oversized},
            HTTP_HOST=PORTAL_HOST,
        )
    assert invalid.status_code == 200
    assert invalid.context["search_form"].errors
    assert invalid.context["users"] == ()
    assert oversized.encode() not in invalid.content
    # Session and authenticated-user resolution remain; the directory itself is not queried.
    assert len(invalid_queries) <= 2


def test_admin_user_cursor_rejects_tampering_and_search_context_reuse(
    admin_identity: tuple[User, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agora.portal.views.USER_PAGE_SIZE", 1)
    admin, password = admin_identity
    User.objects.create_user("CURSOR.USER")
    client = Client()
    login_client(client, admin.soeid, password)

    first = client.get(reverse("admin-user-list"), HTTP_HOST=PORTAL_HOST)
    cursor = first.context["user_page"].next_cursor
    assert cursor is not None
    tampered = f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}"
    assert (
        client.get(
            reverse("admin-user-list"),
            {"cursor": tampered},
            HTTP_HOST=PORTAL_HOST,
        ).status_code
        == 404
    )
    assert (
        client.get(
            reverse("admin-user-list"),
            {"query": "CURSOR", "cursor": cursor},
            HTTP_HOST=PORTAL_HOST,
        ).status_code
        == 404
    )


def test_unauthenticated_admin_redirect_has_a_safe_local_next() -> None:
    client = Client()
    response = client.get(
        f"{reverse('admin-user-list')}?next=https%3A%2F%2Fevil.example",
        HTTP_HOST=PORTAL_HOST,
    )
    assert response.status_code == 302
    assert response["Location"] == "/login/?next=/admin/users/"


def test_user_database_trigger_advances_auth_version_for_raw_active_updates(
    admin_identity: tuple[User, str],
) -> None:
    user = User.objects.create_user("RAW.USER")
    original = user.auth_version
    User.objects.filter(id=user.id).update(is_active=False)
    user.refresh_from_db()
    assert user.auth_version == original + 1
    User.objects.filter(id=user.id).update(is_active=True)
    user.refresh_from_db()
    assert user.auth_version == original + 2

    with pytest.raises(DatabaseError), transaction.atomic():
        User.objects.filter(id=user.id).update(auth_version=1)


def test_throttle_is_bounded_and_expires_without_permanent_lockout(
    admin_identity: tuple[User, str],
) -> None:
    admin, password = admin_identity
    factory = RequestFactory()
    request = factory.post(reverse("login"), REMOTE_ADDR="192.0.2.77")
    wrong = strong_password()
    for _ in range(identity.AUTH_FAILURE_LIMIT):
        assert authenticate_login(request, "ADMIN.1", wrong) is None
    blocked = authenticate_login(request, "ADMIN.1", password)
    assert blocked is None
    assert LoginThrottle.objects.count() == 2

    LoginThrottle.objects.all().update(
        blocked_until=timezone.now() - identity.AUTH_LOCKOUT_DURATION
    )
    recovered = authenticate_login(request, " admin.1 ", password)
    assert recovered is not None
    assert recovered.id == admin.id
    assert LoginThrottle.objects.count() == 0


def test_malformed_login_is_counted_without_persisting_raw_input() -> None:
    factory = RequestFactory()
    request = factory.post(reverse("login"), REMOTE_ADDR="192.0.2.88")
    malformed = "not an identity / with a secret-looking value"
    assert authenticate_login(request, malformed, strong_password()) is None
    assert LoginThrottle.objects.count() == 1
    assert all(malformed not in bucket.bucket_hash for bucket in LoginThrottle.objects.all())


def test_identity_events_are_append_only_and_content_free(
    admin_identity: tuple[User, str],
    regular_identity: tuple[User, str],
) -> None:
    admin, admin_password = admin_identity
    target, target_password = regular_identity
    client = Client()
    login_client(client, "ADMIN.1", admin_password)
    login_client(Client(), "PERSON.1", target_password)
    disable_user(actor_id=admin.id, target_id=target.id)
    enable_user(actor_id=admin.id, target_id=target.id)
    reset_user_password(actor_id=admin.id, target_id=target.id, password=strong_password())
    client.post(reverse("logout"), HTTP_HOST=PORTAL_HOST)

    expected = {
        "auth.bootstrap",
        "user.created",
        "auth.login.succeeded",
        "user.disabled",
        "user.enabled",
        "user.password_reset",
        "auth.logout",
    }
    assert expected.issubset(set(AuditEvent.objects.values_list("event_type", flat=True)))
    for event in AuditEvent.objects.all():
        assert event.metadata == {}
        assert "password" not in str(event.metadata).lower()
        assert "session" not in str(event.metadata).lower()
        assert "token" not in str(event.metadata).lower()


def test_portal_does_not_honor_remote_identity_headers(
    admin_identity: tuple[User, str],
) -> None:
    _, password = admin_identity
    client = Client()
    response = client.post(
        reverse("login"),
        {"soeid": "NOT.PROVISIONED", "password": password},
        HTTP_HOST=PORTAL_HOST,
        HTTP_REMOTE_USER="ADMIN.1",
        HTTP_X_SOEID="ADMIN.1",
    )
    assert response.status_code == 200
    assert b"Sign-in failed. Check your SOEID and password." in response.content
    assert b"Welcome back" not in response.content


def test_missing_target_and_non_admin_service_calls_fail_closed(
    admin_identity: tuple[User, str],
) -> None:
    admin, _ = admin_identity
    non_admin = User.objects.create_user("PERSON.2")
    unknown = UUID("00000000-0000-0000-0000-000000000001")

    with pytest.raises(NotAdministrator):
        enable_user(actor_id=non_admin.id, target_id=admin.id)
    with pytest.raises(NotAdministrator):
        reset_user_password(actor_id=non_admin.id, target_id=admin.id, password=strong_password())
    with pytest.raises(NotAdministrator):
        disable_user(actor_id=non_admin.id, target_id=unknown)

    with pytest.raises(identity.UserNotFound):
        enable_user(actor_id=admin.id, target_id=unknown)
    with pytest.raises(identity.UserNotFound):
        reset_user_password(actor_id=admin.id, target_id=unknown, password=strong_password())


def test_oracle_connection_is_the_expected_vendor() -> None:
    assert connection.vendor == "oracle"


def test_identity_edge_paths_and_throttle_window_reset(
    admin_identity: tuple[User, str],
    regular_identity: tuple[User, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, _ = admin_identity
    target, _ = regular_identity
    unknown = UUID("00000000-0000-0000-0000-000000000002")

    with pytest.raises(UserNotFound):
        disable_user(actor_id=admin.id, target_id=unknown)
    assert enable_user(actor_id=admin.id, target_id=admin.id).id == admin.id

    disable_user(actor_id=admin.id, target_id=target.id)
    assert disable_user(actor_id=admin.id, target_id=target.id).is_active is False
    with pytest.raises(PasswordPolicyError):
        reset_user_password(
            actor_id=admin.id,
            target_id=target.id,
            password="x" * (identity.MAX_PASSWORD_LENGTH + 1),
        )

    factory = RequestFactory()
    request = factory.post(reverse("login"), REMOTE_ADDR="192.0.2.99")
    assert authenticate_login(request, "ADMIN.1", strong_password()) is None
    bucket = LoginThrottle.objects.get(bucket_hash=identity._bucket_hash("ip", "192.0.2.99"))
    LoginThrottle.objects.filter(id=bucket.id).update(
        window_started_at=timezone.now() - identity.AUTH_FAILURE_WINDOW,
        failed_attempts=identity.AUTH_FAILURE_LIMIT,
        blocked_until=timezone.now() + identity.AUTH_LOCKOUT_DURATION,
    )
    assert authenticate_login(request, "ADMIN.1", strong_password()) is None
    bucket.refresh_from_db()
    assert bucket.failed_attempts == 1
    assert bucket.blocked_until is None

    def fail_save(self: User, *args: object, **kwargs: object) -> NoReturn:
        raise IntegrityError("simulated non-duplicate persistence failure")

    monkeypatch.setattr(User, "save", fail_save)
    with pytest.raises(IntegrityError, match="simulated"):
        provision_user(
            actor_id=admin.id,
            soeid="PERSISTENCE.FAILURE",
            password=strong_password(),
        )


def test_portal_forms_and_redirect_guard_reject_unsafe_inputs() -> None:
    invalid_soeid = ProvisionUserForm(
        {
            "soeid": "!!!",
            "password": strong_password(),
            "password_confirmation": strong_password(),
        }
    )
    assert invalid_soeid.is_valid() is False
    assert "soeid" in invalid_soeid.errors

    password = strong_password()
    mismatched_provision = ProvisionUserForm(
        {
            "soeid": "FORM.USER",
            "password": password,
            "password_confirmation": strong_password(),
        }
    )
    assert mismatched_provision.is_valid() is False
    assert "password_confirmation" in mismatched_provision.errors

    mismatched_reset = ResetPasswordForm(
        {"password": password, "password_confirmation": strong_password()}
    )
    assert mismatched_reset.is_valid() is False
    assert "password_confirmation" in mismatched_reset.errors

    request = RequestFactory().get(reverse("login"), HTTP_HOST=PORTAL_HOST)
    assert safe_next_url(request, "/" + ("a" * 4096)) == "/"


def test_portal_views_cover_generic_and_administrator_error_paths(
    admin_identity: tuple[User, str],
    regular_identity: tuple[User, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, admin_password = admin_identity
    target, _ = regular_identity
    client = Client()
    login_client(client, "ADMIN.1", admin_password)

    already_authenticated = client.get(
        f"{reverse('login')}?next=/admin/users/", HTTP_HOST=PORTAL_HOST
    )
    assert already_authenticated.status_code == 302
    assert already_authenticated["Location"] == "/admin/users/"

    malformed = Client().post(reverse("login"), {}, HTTP_HOST=PORTAL_HOST)
    assert malformed.status_code == 200
    assert b"Sign-in failed. Check your SOEID and password." in malformed.content

    anonymous_logout = Client().post(reverse("logout"), HTTP_HOST=PORTAL_HOST)
    assert anonymous_logout.status_code == 302

    assert client.get(reverse("admin-user-create"), HTTP_HOST=PORTAL_HOST).status_code == 200
    duplicate_password = strong_password()
    duplicate = client.post(
        reverse("admin-user-create"),
        {
            "soeid": " person.1 ",
            "password": duplicate_password,
            "password_confirmation": duplicate_password,
        },
        HTTP_HOST=PORTAL_HOST,
    )
    assert duplicate.status_code == 200
    assert b"already exists" in duplicate.content

    weak = client.post(
        reverse("admin-user-create"),
        {"soeid": "WEAK.USER", "password": "short", "password_confirmation": "short"},
        HTTP_HOST=PORTAL_HOST,
    )
    assert weak.status_code == 200
    assert b"at least 12" in weak.content

    last_admin = client.post(
        reverse("admin-user-disable", args=[admin.id]),
        {"confirm": "on"},
        HTTP_HOST=PORTAL_HOST,
    )
    assert last_admin.status_code == 200
    assert b"last active administrator" in last_admin.content

    disable_user(actor_id=admin.id, target_id=target.id)
    already_disabled = client.post(
        reverse("admin-user-disable", args=[target.id]),
        {"confirm": "on"},
        HTTP_HOST=PORTAL_HOST,
    )
    assert already_disabled.status_code == 302
    assert (
        b"already disabled" in client.get(reverse("admin-user-list"), HTTP_HOST=PORTAL_HOST).content
    )

    assert (
        client.get(
            reverse("admin-user-reset-password", args=[target.id]), HTTP_HOST=PORTAL_HOST
        ).status_code
        == 200
    )
    weak_reset = client.post(
        reverse("admin-user-reset-password", args=[target.id]),
        {"password": "short", "password_confirmation": "short"},
        HTTP_HOST=PORTAL_HOST,
    )
    assert weak_reset.status_code == 200
    assert b"at least 12" in weak_reset.content

    enabled = client.post(reverse("admin-user-enable", args=[target.id]), HTTP_HOST=PORTAL_HOST)
    assert enabled.status_code == 302
    assert (
        client.post(
            reverse("admin-user-enable", args=[target.id]), HTTP_HOST=PORTAL_HOST
        ).status_code
        == 302
    )

    self_reset_password = strong_password()
    self_reset = client.post(
        reverse("admin-user-reset-password", args=[admin.id]),
        {"password": self_reset_password, "password_confirmation": self_reset_password},
        HTTP_HOST=PORTAL_HOST,
    )
    assert self_reset.status_code == 302
    assert User.objects.get(id=admin.id).check_password(self_reset_password)

    second_admin = provision_user(
        actor_id=admin.id,
        soeid="SECOND.ADMIN",
        password=strong_password(),
        is_administrator=True,
    )
    self_disable = client.post(
        reverse("admin-user-disable", args=[admin.id]),
        {"confirm": "on"},
        HTTP_HOST=PORTAL_HOST,
    )
    assert self_disable.status_code == 200
    assert b"another administrator" in self_disable.content

    def reject_provision(*args: object, **kwargs: object) -> NoReturn:
        raise NotAdministrator

    monkeypatch.setattr("agora.portal.views.provision_user", reject_provision)
    denied_create_password = strong_password()
    denied_create = client.post(
        reverse("admin-user-create"),
        {
            "soeid": "DENIED.USER",
            "password": denied_create_password,
            "password_confirmation": denied_create_password,
        },
        HTTP_HOST=PORTAL_HOST,
    )
    assert denied_create.status_code == 403

    def reject_disable(*args: object, **kwargs: object) -> NoReturn:
        raise UserNotFound

    monkeypatch.setattr("agora.portal.views.disable_user", reject_disable)
    denied_disable = client.post(
        reverse("admin-user-disable", args=[target.id]),
        {"confirm": "on"},
        HTTP_HOST=PORTAL_HOST,
    )
    assert denied_disable.status_code == 403

    def reject_enable(*args: object, **kwargs: object) -> NoReturn:
        raise NotAdministrator

    monkeypatch.setattr("agora.portal.views.enable_user", reject_enable)
    denied_enable = client.post(
        reverse("admin-user-enable", args=[target.id]), HTTP_HOST=PORTAL_HOST
    )
    assert denied_enable.status_code == 403

    def reject_reset(*args: object, **kwargs: object) -> NoReturn:
        raise UserNotFound

    monkeypatch.setattr("agora.portal.views.reset_user_password", reject_reset)
    denied_reset_password = strong_password()
    denied_reset = client.post(
        reverse("admin-user-reset-password", args=[target.id]),
        {
            "password": denied_reset_password,
            "password_confirmation": denied_reset_password,
        },
        HTTP_HOST=PORTAL_HOST,
    )
    assert denied_reset.status_code == 403
    assert second_admin.is_active
