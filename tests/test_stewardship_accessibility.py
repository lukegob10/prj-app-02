from __future__ import annotations

from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from django.contrib.auth.models import AnonymousUser
from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.test import RequestFactory

from agora.portal.forms import GrantViewerForm
from agora.portal.stewardship_forms import (
    DashboardAccessRequestForm,
    TransferOwnershipConfirmForm,
    TransferOwnershipForm,
)

TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "agora"
    / "portal"
    / "templates"
    / "portal"
    / "projects"
)
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000123")
GRANT_ID = UUID("00000000-0000-0000-0000-000000000456")


class DocumentParser(HTMLParser):
    """Capture the semantic surface needed for template accessibility checks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.text_parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))

    def attributes_for(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for element, attrs in self.elements if element == tag]

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.text_parts).split())


def render_template(template_name: str, context: dict[str, object]) -> str:
    request = RequestFactory().get("/")
    request.user = AnonymousUser()
    get_token(request)
    return render_to_string(template_name, context, request=request)


def parse(document: str) -> DocumentParser:
    parser = DocumentParser()
    parser.feed(document)
    return parser


def assert_no_inline_behavior(document: str, parser: DocumentParser) -> None:
    assert parser.attributes_for("script") == []
    assert parser.attributes_for("style") == []
    assert ' style="' not in document
    assert all(
        not attribute.lower().startswith("on")
        for _, attributes in parser.elements
        for attribute in attributes
    )


def project() -> SimpleNamespace:
    return SimpleNamespace(id=PROJECT_ID, name="Quarterly risk dashboard")


def empty_page(*, previous_url: str | None = None, next_url: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(previous_url=previous_url, next_url=next_url)


def test_share_panel_exposes_one_labeled_stable_link_and_bounded_access_surfaces() -> None:
    timestamp = datetime(2026, 8, 28, 15, 30, tzinfo=UTC)
    active_viewer = SimpleNamespace(soeid="VIEWER.ACTIVE", is_active=True)
    disabled_viewer = SimpleNamespace(soeid="VIEWER.DISABLED", is_active=False)
    owner = SimpleNamespace(soeid="OWNER.ONE")
    active_grant = SimpleNamespace(
        id=GRANT_ID,
        viewer=disabled_viewer,
        created_at=timestamp,
        created_by=owner,
    )
    historical_grant = SimpleNamespace(
        viewer=active_viewer,
        created_at=timestamp,
        created_by=owner,
        revoked_at=timestamp,
        revoked_by=owner,
    )
    stable_url = f"https://portal.agora.test/projects/{PROJECT_ID}/view/?source=share&mode=full"
    document = render_template(
        "portal/projects/access.html",
        {
            "project": project(),
            "owner_soeid": owner.soeid,
            "stable_view_url": stable_url,
            "effective_access_page_count": 3,
            "form": GrantViewerForm(),
            "grant_url": f"/projects/{PROJECT_ID}/access/",
            "active_grants": (active_grant,),
            "active_grants_page": empty_page(next_url="?active=signed-next"),
            "grant_history": (historical_grant,),
            "grant_history_page": empty_page(previous_url="?history=signed-previous"),
        },
    )
    parser = parse(document)

    assert len(parser.attributes_for("h1")) == 1
    stable_input = next(
        attrs for attrs in parser.attributes_for("input") if attrs.get("id") == "stable-view-url"
    )
    assert stable_input == {
        "class": "portal-input",
        "id": "stable-view-url",
        "type": "url",
        "value": stable_url,
        "aria-describedby": "stable-view-url-help",
        "readonly": None,
    }
    assert any(label.get("for") == "stable-view-url" for label in parser.attributes_for("label"))
    assert "use your device's copy command" in parser.text
    assert "OWNER.ONE · Full control" in parser.text
    assert "3 active accounts with a current Viewer grant" in parser.text
    assert "summary covers only the bounded page below" in parser.text
    assert "Account disabled" in parser.text

    table_regions = [
        attrs for attrs in parser.attributes_for("div") if attrs.get("class") == "portal-table-wrap"
    ]
    assert len(table_regions) == 2
    assert all(region.get("role") == "region" for region in table_regions)
    assert all(region.get("tabindex") == "0" for region in table_regions)
    assert len(parser.attributes_for("table")) == 2
    assert len(parser.attributes_for("caption")) == 2
    assert "up to 25 per page" in parser.text

    history = next(
        attrs for attrs in parser.attributes_for("details") if attrs.get("class") == "portal-card"
    )
    assert history.get("open") is None
    assert "Retained access history" in parser.text
    assert "Open request queue" in parser.text
    assert "Start ownership transfer" in parser.text
    hrefs = {attrs.get("href") for attrs in parser.attributes_for("a")}
    assert f"/projects/{PROJECT_ID}/access/requests/" in hrefs
    assert f"/projects/{PROJECT_ID}/transfer/" in hrefs

    post_forms = [attrs for attrs in parser.attributes_for("form") if attrs.get("method") == "post"]
    assert len(post_forms) == 1
    assert post_forms[0].get("action") == f"/projects/{PROJECT_ID}/access/"
    assert any(
        attrs.get("name") == "csrfmiddlewaretoken" for attrs in parser.attributes_for("input")
    )
    assert_no_inline_behavior(document, parser)


def test_request_access_form_is_generic_labeled_escaped_and_metadata_free() -> None:
    request_form = DashboardAccessRequestForm(initial={"message": "<script>ask safely</script>"})
    document = render_template(
        "portal/projects/request_access.html",
        {
            "request_form": request_form,
            "request_url": f"/projects/{PROJECT_ID}/view/",
            "request_submitted": False,
            "project": SimpleNamespace(name="SECRET PROJECT NAME"),
            "owner_soeid": "SECRET.OWNER",
            "description": "SECRET DESCRIPTION",
            "tags": ("SECRET TAG",),
            "publication_state": "SECRET PUBLICATION",
        },
    )
    parser = parse(document)

    assert len(parser.attributes_for("h1")) == 1
    assert "Project unavailable" in parser.text
    assert "does not exist or is not available to your SOEID" in parser.text
    assert "SECRET PROJECT NAME" not in document
    assert "SECRET.OWNER" not in document
    assert "SECRET DESCRIPTION" not in document
    assert "SECRET TAG" not in document
    assert "SECRET PUBLICATION" not in document
    assert "&lt;script&gt;ask safely&lt;/script&gt;" in document
    assert "<script>ask safely</script>" not in document

    message = next(
        attrs for attrs in parser.attributes_for("textarea") if attrs.get("id") == "id_message"
    )
    assert message.get("maxlength") == "500"
    assert message.get("aria-describedby") == "access-request-message-help"
    assert any(label.get("for") == "id_message" for label in parser.attributes_for("label"))
    assert "owner can read this message" in parser.text

    forms = parser.attributes_for("form")
    assert len(forms) == 1
    assert forms[0].get("method") == "post"
    assert forms[0].get("action") == f"/projects/{PROJECT_ID}/view/"
    assert any(
        attrs.get("name") == "csrfmiddlewaretoken" for attrs in parser.attributes_for("input")
    )
    assert_no_inline_behavior(document, parser)


def test_submitted_request_uses_exact_safe_copy_and_never_echoes_the_note() -> None:
    request_form = DashboardAccessRequestForm({"message": "PRIVATE NOTE THAT MUST NOT BE ECHOED"})
    assert request_form.is_valid()
    document = render_template(
        "portal/projects/request_access.html",
        {
            "request_form": request_form,
            "request_url": f"/projects/{PROJECT_ID}/view/",
            "request_submitted": True,
            "project": SimpleNamespace(name="SECRET PROJECT NAME"),
            "owner_soeid": "SECRET.OWNER",
        },
    )
    parser = parse(document)

    confirmation = "Request received. If this dashboard can accept requests, its owner will see it."
    assert confirmation in parser.text
    assert "PRIVATE NOTE THAT MUST NOT BE ECHOED" not in document
    assert "SECRET PROJECT NAME" not in document
    assert "SECRET.OWNER" not in document
    assert parser.attributes_for("form") == []
    assert any(attrs.get("role") == "status" for attrs in parser.attributes_for("div"))
    assert "does not create duplicate pending requests" in parser.text
    assert_no_inline_behavior(document, parser)


def test_owner_request_queue_names_scroll_region_and_each_post_decision() -> None:
    access_request = SimpleNamespace(
        id=42,
        requester=SimpleNamespace(soeid="REQUESTER.ONE", is_active=False),
        message="<script>not markup</script>",
        requested_at=datetime(2026, 8, 28, 16, 0, tzinfo=UTC),
    )
    document = render_template(
        "portal/projects/access_requests.html",
        {
            "project": project(),
            "access_requests": (access_request,),
            "request_page": empty_page(next_url="?request_cursor=signed-next"),
            "access_url": f"/projects/{PROJECT_ID}/access/",
        },
    )
    parser = parse(document)

    assert len(parser.attributes_for("h1")) == 1
    assert "Pending only" in parser.text
    assert "up to 25 per page" in parser.text
    assert "Disabled account" in parser.text
    assert "Approval is blocked" in parser.text
    assert "&lt;script&gt;not markup&lt;/script&gt;" in document
    assert "<script>not markup</script>" not in document
    region = next(
        attrs for attrs in parser.attributes_for("div") if attrs.get("class") == "portal-table-wrap"
    )
    assert region == {
        "class": "portal-table-wrap",
        "role": "region",
        "aria-labelledby": "access-requests-caption",
        "tabindex": "0",
    }
    assert len(parser.attributes_for("caption")) == 1

    post_forms = [attrs for attrs in parser.attributes_for("form") if attrs.get("method") == "post"]
    assert {form.get("action") for form in post_forms} == {
        f"/projects/{PROJECT_ID}/access/requests/42/approve/",
        f"/projects/{PROJECT_ID}/access/requests/42/decline/",
    }
    csrf_inputs = [
        attrs
        for attrs in parser.attributes_for("input")
        if attrs.get("name") == "csrfmiddlewaretoken"
    ]
    assert len(csrf_inputs) == 2
    assert "Give access to REQUESTER.ONE" in parser.text
    assert "Deny request from REQUESTER.ONE" in parser.text
    assert "Next pending access requests" in parser.text
    assert_no_inline_behavior(document, parser)


def test_transfer_selection_has_one_labeled_primary_post_and_safe_errors() -> None:
    form = TransferOwnershipForm({"incoming_owner_soeid": "not valid!"})
    assert form.is_valid() is False
    form.fields["incoming_owner_soeid"].widget.attrs["aria-describedby"] = (
        "incoming-owner-soeid-help incoming-owner-soeid-error"
    )
    form.fields["incoming_owner_soeid"].widget.attrs["aria-invalid"] = "true"
    document = render_template(
        "portal/projects/transfer_ownership.html",
        {
            "project": project(),
            "form": form,
            "transfer_url": f"/projects/{PROJECT_ID}/transfer/",
            "access_url": f"/projects/{PROJECT_ID}/access/",
        },
    )
    parser = parse(document)

    assert len(parser.attributes_for("h1")) == 1
    target = next(
        attrs
        for attrs in parser.attributes_for("input")
        if attrs.get("name") == "incoming_owner_soeid"
    )
    assert target.get("aria-invalid") == "true"
    assert target.get("aria-describedby") == (
        "incoming-owner-soeid-help incoming-owner-soeid-error"
    )
    assert any(label.get("for") == target.get("id") for label in parser.attributes_for("label"))
    assert any(
        attrs.get("id") == "incoming-owner-soeid-error" and attrs.get("role") == "alert"
        for attrs in parser.attributes_for("div")
    )
    forms = parser.attributes_for("form")
    assert len(forms) == 1
    assert forms[0].get("method") == "post"
    assert forms[0].get("action") == f"/projects/{PROJECT_ID}/transfer/"
    buttons = parser.attributes_for("button")
    assert buttons == [{"class": "portal-button portal-button--primary", "type": "submit"}]
    assert "Review transfer" in parser.text
    assert_no_inline_behavior(document, parser)


def test_transfer_confirmation_names_every_effect_and_requires_one_confirmation() -> None:
    form = TransferOwnershipConfirmForm({})
    assert form.is_valid() is False
    form.fields["confirm"].widget.attrs["aria-describedby"] = "transfer-confirm-error"
    form.fields["confirm"].widget.attrs["aria-invalid"] = "true"
    document = render_template(
        "portal/projects/transfer_ownership_confirm.html",
        {
            "project": project(),
            "incoming_owner_soeid": "OWNER.INCOMING",
            "form": form,
            "confirmation_token": "opaque&<signed-token>",
            "confirm_url": f"/projects/{PROJECT_ID}/transfer/confirm/",
            "access_url": f"/projects/{PROJECT_ID}/access/",
        },
    )
    parser = parse(document)

    assert len(parser.attributes_for("h1")) == 1
    for expected in (
        "OWNER.INCOMING becomes the owner with Full control",
        "immediately lose management, preview, published-owner view",
        "access-request queue, aggregate usage, and existing render authority",
        "Each fails on its next authorization check",
        "stable viewer URL",
        "revision history",
        "pinned Published revision, publication state, and Tags do not change",
        "Unrelated Viewer grants do not change",
        "closed and retained in access history",
        "pending access request from OWNER.INCOMING becomes Approved",
        "All other pending requests remain for the new owner",
        "Historical creators, grantors, and revokers remain unchanged",
        "cannot be undone by rewriting history",
        "transfer-back is a new ownership action",
        "does not restore earlier authority or recreate the revoked Viewer grant",
    ):
        assert expected in parser.text

    forms = parser.attributes_for("form")
    assert len(forms) == 1
    assert forms[0].get("method") == "post"
    assert forms[0].get("action") == f"/projects/{PROJECT_ID}/transfer/confirm/"
    token = next(
        attrs
        for attrs in parser.attributes_for("input")
        if attrs.get("name") == "confirmation_token"
    )
    assert token.get("type") == "hidden"
    assert token.get("value") == "opaque&<signed-token>"
    checkbox = next(
        attrs for attrs in parser.attributes_for("input") if attrs.get("name") == "confirm"
    )
    assert checkbox.get("aria-invalid") == "true"
    assert checkbox.get("aria-describedby") == "transfer-confirm-error"
    assert any(label.get("for") == checkbox.get("id") for label in parser.attributes_for("label"))
    buttons = parser.attributes_for("button")
    assert buttons == [{"class": "portal-button portal-button--danger", "type": "submit"}]
    assert_no_inline_behavior(document, parser)


def test_stewardship_templates_reuse_responsive_classes_without_inline_layout() -> None:
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in (
            TEMPLATE_ROOT / "access.html",
            TEMPLATE_ROOT / "access_requests.html",
            TEMPLATE_ROOT / "request_access.html",
            TEMPLATE_ROOT / "transfer_ownership.html",
            TEMPLATE_ROOT / "transfer_ownership_confirm.html",
        )
    }

    for source in sources.values():
        assert "<script" not in source
        assert "<style" not in source
        assert ' style="' not in source
        assert "onclick=" not in source
        assert "|safe" not in source

    assert "portal-card-grid portal-card-grid--three" in sources["access.html"]
    assert sources["access.html"].count('class="portal-table-wrap" role="region"') == 2
    assert 'class="portal-table-wrap" role="region"' in sources["access_requests.html"]
    assert 'tabindex="0"' in sources["access.html"]
    assert 'tabindex="0"' in sources["access_requests.html"]
    assert "<details" in sources["access.html"]
    assert "<summary" in sources["access.html"]
    assert 'type="url"' in sources["access.html"]
    assert " readonly" in sources["access.html"]
    assert "effective_viewer_count" not in sources["access.html"]
    assert "effective_access_count" not in sources["access.html"]

    generic_source = sources["request_access.html"]
    for metadata_variable in (
        "{{ project",
        "{{ owner_soeid",
        "{{ description",
        "{{ tags",
        "{{ publication",
    ):
        assert metadata_variable not in generic_source
    assert "{{ request_form.message }}" in generic_source
    assert "{% csrf_token %}" in generic_source
    assert "{% csrf_token %}" in sources["access_requests.html"]
    assert "{% csrf_token %}" in sources["transfer_ownership.html"]
    assert "{% csrf_token %}" in sources["transfer_ownership_confirm.html"]
