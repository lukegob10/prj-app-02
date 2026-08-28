from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from urllib.request import Request

import pytest

from scripts.load import agora_capacity as capacity
from scripts.load.agora_capacity import (
    Credentials,
    Observation,
    ObservationRecorder,
    ProfileError,
    RequestSpec,
    build_static_request_shapes,
    classify_transport_error,
    extract_csrf_token,
    main,
    parse_nonnegative_metric_header,
    parse_profile_document,
    percentile_summary,
    resolve_credentials,
    summarize_observations,
)


def _profile_document(**overrides: object) -> bytes:
    profile: dict[str, object] = {
        "schema_version": 1,
        "capacity_targets_status": "UNVERIFIED",
        "portal_origin": "https://portal.agora.example",
        "content_origin": "https://content.agorausercontent.example",
        "dashboard_id": "00000000-0000-4000-8000-000000000001",
        "csv_logical_name": "data.csv",
        "owner": {
            "soeid_env": "AGORA_LOAD_OWNER_SOEID",
            "password_env": "AGORA_LOAD_OWNER_PASSWORD",
        },
        "viewer": {
            "soeid_env": "AGORA_LOAD_VIEWER_SOEID",
            "password_env": "AGORA_LOAD_VIEWER_PASSWORD",
        },
        "workload": {
            "virtual_users": 4,
            "ramp_seconds": 0,
            "hold_seconds": 1,
            "mutation_cycles": 1,
            "request_timeout_seconds": 2,
            "propagation_timeout_seconds": 1,
            "propagation_poll_ms": 10,
            "think_time_ms": 0,
            "seed": 1234,
            "read_weights": {
                "shared_with_me": 4,
                "grant_check": 4,
                "render_delivery": 1,
            },
        },
        "telemetry_headers": {
            "oracle_query_ms": "X-Agora-Oracle-Query-Ms",
            "oracle_pool_wait_ms": "X-Agora-Oracle-Pool-Wait-Ms",
        },
    }
    profile.update(overrides)
    return json.dumps(profile).encode()


def test_profile_validation_is_strict_and_capacity_status_stays_unverified() -> None:
    profile = parse_profile_document(_profile_document())

    assert profile.workload.virtual_users == 4
    assert profile.workload.seed == 1234
    assert profile.csv_logical_name == "data.csv"
    assert "00000000" not in repr(profile)

    with pytest.raises(ProfileError, match="must be UNVERIFIED"):
        parse_profile_document(_profile_document(capacity_targets_status="verified"))
    with pytest.raises(ProfileError, match="must use HTTPS"):
        parse_profile_document(_profile_document(portal_origin="http://portal.agora.example"))
    with pytest.raises(ProfileError, match="narrow basename"):
        parse_profile_document(_profile_document(csv_logical_name="../private.csv"))

    document = json.loads(_profile_document())
    document["token-private-key"] = "ignored"
    with pytest.raises(ProfileError) as raised:
        parse_profile_document(json.dumps(document).encode())
    assert "token-private-key" not in str(raised.value)


def test_loopback_http_requires_an_explicit_validation_override() -> None:
    document = _profile_document(
        portal_origin="http://localhost:8000",
        content_origin="http://127.0.0.1:8001",
    )

    with pytest.raises(ProfileError, match="must use HTTPS"):
        parse_profile_document(document)

    profile = parse_profile_document(document, allow_http_loopback=True)
    assert profile.portal_origin.value == "http://localhost:8000"
    assert profile.content_origin.value == "http://127.0.0.1:8001"


def test_credentials_resolve_only_from_environment_and_never_appear_in_repr() -> None:
    profile = parse_profile_document(_profile_document())
    environment = {
        "AGORA_LOAD_OWNER_SOEID": "SYNTH.OWNER",
        "AGORA_LOAD_OWNER_PASSWORD": "owner-secret-value",
        "AGORA_LOAD_VIEWER_SOEID": "SYNTH.VIEWER",
        "AGORA_LOAD_VIEWER_PASSWORD": "viewer-secret-value",
    }

    owner, viewer = resolve_credentials(profile, environment)

    assert owner == Credentials("SYNTH.OWNER", "owner-secret-value")
    assert viewer == Credentials("SYNTH.VIEWER", "viewer-secret-value")
    assert repr(owner) == "Credentials(soeid=<redacted>, password=<redacted>)"
    assert "SYNTH" not in repr(owner)
    assert "secret" not in repr(owner)

    with pytest.raises(ProfileError, match="viewer password environment variable"):
        resolve_credentials(
            profile,
            {
                key: value
                for key, value in environment.items()
                if key != "AGORA_LOAD_VIEWER_PASSWORD"
            },
        )


def test_request_shapes_are_narrow_and_sensitive_fields_are_never_represented() -> None:
    profile = parse_profile_document(_profile_document())

    shapes = build_static_request_shapes(profile)

    assert set(shapes) == {"shared_with_me", "grant_check", "render_start"}
    assert shapes["shared_with_me"].path == "/projects/?scope=shared"
    assert shapes["grant_check"].method == "GET"
    assert shapes["render_start"].capture_body
    assert all(shape.origin == "portal" for shape in shapes.values())
    assert all(shape.form is None for shape in shapes.values())

    sensitive = RequestSpec(
        scenario="signed_in_session",
        origin="portal",
        method="POST",
        path="/render/viewer/a-private-token/",
        expected_statuses=frozenset({302}),
        form={"password": "do-not-log", "csrfmiddlewaretoken": "also-private"},
    )
    rendered = repr(sensitive)
    assert "a-private-token" not in rendered
    assert "do-not-log" not in rendered
    assert "also-private" not in rendered
    assert "<redacted>" in rendered


def test_portal_form_requests_supply_exact_https_csrf_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = parse_profile_document(_profile_document())
    recorder = ObservationRecorder(tmp_path / "events.jsonl", seed=1, profile_digest="a" * 64)
    session = capacity.HttpSession(profile, recorder, capacity.RequestIdFactory(1))
    captured: dict[str, object] = {}

    class Response:
        status = 302

        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, amount: int = -1) -> bytes:
            return b""

    def open_request(request: object, *, timeout: float) -> Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(session._opener, "open", open_request)
    try:
        result = session.request(
            RequestSpec(
                scenario="signed_in_session",
                origin="portal",
                method="POST",
                path="/login/",
                expected_statuses=frozenset({302}),
                form={"csrfmiddlewaretoken": "A" * 64, "password": "private"},
            )
        )
    finally:
        recorder.__exit__(None, None, None)

    request = cast(Request, captured["request"])
    assert result.observation.expected is True
    assert request.get_header("Origin") == "https://portal.agora.example"
    assert request.get_header("Referer") == "https://portal.agora.example/login/"


def test_observation_allowlist_and_metric_summaries_are_deterministic() -> None:
    observations = [
        Observation(
            request_id=f"request-{index}",
            scenario="shared_with_me",
            status_code=200 if index < 4 else 503,
            duration_ms=duration,
            response_bytes=index * 10,
            expected=index < 4,
            first_byte_ms=duration / 4,
            oracle_query_ms=duration / 2 if index != 2 else None,
            oracle_pool_wait_ms=1.0 if index != 2 else None,
            error_kind=None if index < 4 else "unexpected_status",
        )
        for index, duration in enumerate((10.0, 20.0, 30.0, 40.0, 50.0))
    ]

    safe_event = observations[0].safe_mapping()
    assert set(safe_event) == {
        "record_type",
        "request_id",
        "scenario",
        "status_code",
        "duration_ms",
        "first_byte_ms",
        "response_bytes",
        "expected",
        "authorization_outcome",
        "oracle_query_ms",
        "oracle_pool_wait_ms",
        "telemetry_invalid",
        "error_kind",
    }
    assert percentile_summary([50.0, 10.0, 40.0, 20.0, 30.0]) == {
        "samples": 5,
        "p50": 30.0,
        "p95": 50.0,
        "p99": 50.0,
    }

    summary = summarize_observations(observations)["shared_with_me"]
    assert summary["requests"] == 5
    assert summary["errors"] == 1
    assert summary["error_rate"] == 0.2
    assert summary["response_bytes"] == 100
    assert summary["latency_ms"] == {
        "samples": 5,
        "p50": 30.0,
        "p95": 50.0,
        "p99": 50.0,
    }
    assert summary["first_byte_ms"] == {
        "samples": 5,
        "p50": 7.5,
        "p95": 12.5,
        "p99": 12.5,
    }
    oracle_query = cast(dict[str, object], summary["oracle_query_ms"])
    assert oracle_query["samples"] == 4
    assert summary["telemetry_missing_requests"] == 1
    assert summary["authorization_outcomes"] == {"not_applicable": 5}

    authorization = summarize_observations(
        [
            Observation("11111111-1111-4111-8111-111111111111", "grant_check", 200, 1, 1, True),
            Observation(
                "22222222-2222-4222-8222-222222222222",
                "authorization_check",
                404,
                1,
                1,
                True,
            ),
        ]
    )
    grant_summary = authorization["grant_check"]
    check_summary = authorization["authorization_check"]
    assert grant_summary["authorization_outcomes"] == {"allowed": 1}
    assert check_summary["authorization_outcomes"] == {"denied": 1}


def test_telemetry_and_error_classification_never_format_sensitive_exceptions() -> None:
    headers = {
        "X-Agora-Oracle-Query-Ms": "12.5",
        "X-Agora-Oracle-Pool-Wait-Ms": "not-a-number",
    }

    assert parse_nonnegative_metric_header(headers, "X-Agora-Oracle-Query-Ms") == (
        12.5,
        False,
    )
    assert parse_nonnegative_metric_header(headers, "X-Agora-Oracle-Pool-Wait-Ms") == (
        None,
        True,
    )
    assert parse_nonnegative_metric_header(headers, "Missing") == (None, False)
    assert classify_transport_error(TimeoutError("token=private")) == "timeout"
    assert classify_transport_error(OSError("cookie=private")) == "network"


def test_csrf_parser_accepts_pages_with_header_and_page_post_forms() -> None:
    header_token = "A" * 64
    page_token = "B" * 64
    page = (
        '<form method="post"><input name="csrfmiddlewaretoken" value="'
        f'{header_token}"></form><form method="post"><input '
        f'name="csrfmiddlewaretoken" value="{page_token}"></form>'
    ).encode()

    assert extract_csrf_token(page) == header_token


def test_cli_defaults_to_network_free_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    profile_path = tmp_path / "capacity.json"
    profile_path.write_bytes(_profile_document())

    result = main(["--profile", str(profile_path)])

    output = json.loads(capsys.readouterr().out)
    assert result == 0
    assert output["validation"] == "ok"
    assert output["capacity_targets_status"] == "UNVERIFIED"
    assert "portal.agora.example" not in json.dumps(output)
    assert "00000000-0000-4000-8000-000000000001" not in json.dumps(output)


def test_event_output_sanitizes_unexpected_internal_labels(tmp_path: Path) -> None:
    secret = "private-token-cookie-password"
    event_path = tmp_path / "events.jsonl"

    with ObservationRecorder(event_path, seed=1, profile_digest="a" * 64) as recorder:
        recorder.record(
            Observation(
                request_id=secret,
                scenario=secret,
                status_code=None,
                duration_ms=1,
                response_bytes=0,
                expected=False,
                error_kind=secret,
            )
        )
        recorder.finish(seed=1)

    output = event_path.read_text(encoding="utf-8")
    assert secret not in output
    assert "harness_internal" in output
    assert "invalid-request-id" in output
    assert '"error_kind":"internal"' in output


def test_cli_redacts_unexpected_execution_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "https://content.example/render/viewer/private-token/ cookie=private"
    profile_path = tmp_path / "capacity.json"
    profile_path.write_bytes(_profile_document())
    environment = {
        "AGORA_LOAD_OWNER_SOEID": "SYNTH.OWNER",
        "AGORA_LOAD_OWNER_PASSWORD": "owner-secret-value",
        "AGORA_LOAD_VIEWER_SOEID": "SYNTH.VIEWER",
        "AGORA_LOAD_VIEWER_PASSWORD": "viewer-secret-value",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    def fail_capacity(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError(secret)

    monkeypatch.setattr(capacity, "run_capacity", fail_capacity)

    result = capacity.main(
        [
            "--profile",
            str(profile_path),
            "--execute",
            "--allow-mutations",
            "--events-output",
            str(tmp_path / "events.jsonl"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert secret not in captured.out
    assert secret not in captured.err
    assert "owner-secret-value" not in captured.err
    assert "request details were not emitted" in captured.err
