"""Run a redacted, opt-in, production-shaped Agora capacity profile.

This standard-library harness is intentionally not part of the product runtime. It exercises
real signed-in portal sessions and isolated content delivery, but it does not turn a local or
unit-test result into a production capacity claim. Representative Oracle, shared storage, TLS,
and deployment telemetry are required before any target can be verified.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import math
import os
import random
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from http.client import HTTPMessage
from pathlib import Path
from types import TracebackType
from typing import Final, Literal, Protocol, Self
from uuid import NAMESPACE_URL, UUID, uuid5

UNVERIFIED_NOTICE: Final = (
    "UNVERIFIED CAPACITY TARGETS: results require representative Oracle, shared storage, "
    "deployment topology, and observability review."
)
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")
_CSV_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}\.csv\Z", re.IGNORECASE)
_CSRF_TOKEN = re.compile(r"[A-Za-z0-9]{32,128}\Z")
_RENDER_PATH = re.compile(r"/render/viewer/[A-Za-z0-9_-]{43}/\Z")
_REVOKE_PATH = re.compile(r"/projects/[0-9a-fA-F-]{36}/access/[0-9a-fA-F-]{36}/revoke/\Z")
_CAPTURE_LIMIT = 2 * 1024 * 1024
_READ_SCENARIOS = frozenset({"shared_with_me", "grant_check", "render_delivery"})
_AUTHORIZATION_SCENARIOS = frozenset(
    {
        "authorization_check",
        "csv_delivery",
        "grant_check",
        "grant_propagation",
        "html_delivery",
        "render_start",
        "revocation_propagation",
    }
)
_SAFE_ERROR_KINDS = frozenset(
    {
        "configuration",
        "internal",
        "network",
        "response_shape",
        "response_too_large",
        "timeout",
        "unexpected_status",
    }
)
_SAFE_SCENARIOS = frozenset(
    {
        "authorization_check",
        "csv_delivery",
        "grant_check",
        "grant_mutation",
        "grant_prepare",
        "grant_propagation",
        "harness_internal",
        "html_delivery",
        "render_delivery",
        "render_start",
        "render_start_shape",
        "revocation_propagation",
        "revoke_mutation",
        "revoke_prepare",
        "revoke_prepare_shape",
        "shared_with_me",
        "signed_in_session",
        "signed_in_session_csrf",
    }
)


class ProfileError(ValueError):
    """A safe, value-free capacity-profile validation error."""


class ScenarioFailure(RuntimeError):
    """A scenario failed after its request observation was safely recorded."""


class ResponseTooLarge(RuntimeError):
    """A portal response exceeded the bounded transient parser buffer."""


class _ReadableResponse(Protocol):
    def read(self, amount: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class Origin:
    """One normalized, origin-only HTTP endpoint."""

    value: str
    scheme: str
    hostname: str
    port: int | None

    def url(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//"):
            raise ProfileError("request paths must be absolute paths on the configured origin")
        if any(character in path for character in ("\r", "\n", "#")):
            raise ProfileError("request paths cannot contain control characters or fragments")
        return f"{self.value}{path}"

    def checked_path(self, candidate: str) -> str:
        try:
            parsed = urllib.parse.urlsplit(urllib.parse.urljoin(f"{self.value}/", candidate))
            candidate_port = parsed.port
        except ValueError as error:
            raise ScenarioFailure("response_shape") from error
        if (
            parsed.scheme != self.scheme
            or parsed.hostname != self.hostname
            or candidate_port != self.port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ScenarioFailure("response_shape")
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path


@dataclass(frozen=True, slots=True)
class CredentialReference:
    """Environment variable names that point to one synthetic load identity."""

    soeid_env: str
    password_env: str


@dataclass(frozen=True, slots=True, repr=False)
class Credentials:
    """Resolved synthetic credentials that deliberately have no revealing repr."""

    soeid: str
    password: str

    def __repr__(self) -> str:
        return "Credentials(soeid=<redacted>, password=<redacted>)"


@dataclass(frozen=True, slots=True)
class Workload:
    virtual_users: int
    ramp_seconds: float
    hold_seconds: float
    mutation_cycles: int
    request_timeout_seconds: float
    propagation_timeout_seconds: float
    propagation_poll_ms: int
    think_time_ms: int
    seed: int
    read_weights: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class TelemetryHeaders:
    oracle_query_ms: str
    oracle_pool_wait_ms: str


@dataclass(frozen=True, slots=True, repr=False)
class CapacityProfile:
    portal_origin: Origin
    content_origin: Origin
    dashboard_id: UUID
    csv_logical_name: str
    owner: CredentialReference
    viewer: CredentialReference
    workload: Workload
    telemetry: TelemetryHeaders
    source_digest: str

    def __repr__(self) -> str:
        return "CapacityProfile(<redacted production fixture>)"


@dataclass(frozen=True, slots=True, repr=False)
class RequestSpec:
    """One origin-pinned request shape; private paths and forms never appear in repr."""

    scenario: str
    origin: Literal["portal", "content"]
    method: Literal["GET", "POST"]
    path: str
    expected_statuses: frozenset[int]
    capture_body: bool = False
    form: Mapping[str, str] | None = None

    def __repr__(self) -> str:
        return (
            f"RequestSpec(scenario={self.scenario!r}, origin={self.origin!r}, "
            f"method={self.method!r}, path=<redacted>, form=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class Observation:
    request_id: str
    scenario: str
    status_code: int | None
    duration_ms: float
    response_bytes: int
    expected: bool
    first_byte_ms: float | None = None
    oracle_query_ms: float | None = None
    oracle_pool_wait_ms: float | None = None
    telemetry_invalid: bool = False
    error_kind: str | None = None

    def safe_mapping(self) -> dict[str, object]:
        """Return the complete allowlisted event schema and nothing request-derived."""
        return {
            "record_type": "request",
            "request_id": _safe_request_id(self.request_id),
            "scenario": _safe_scenario(self.scenario),
            "status_code": self.status_code,
            "duration_ms": round(self.duration_ms, 3),
            "first_byte_ms": _rounded_or_none(self.first_byte_ms),
            "response_bytes": self.response_bytes,
            "expected": self.expected,
            "authorization_outcome": _authorization_outcome(
                self.scenario,
                self.status_code,
            ),
            "oracle_query_ms": _rounded_or_none(self.oracle_query_ms),
            "oracle_pool_wait_ms": _rounded_or_none(self.oracle_pool_wait_ms),
            "telemetry_invalid": self.telemetry_invalid,
            "error_kind": _safe_error_kind(self.error_kind),
        }


@dataclass(frozen=True, slots=True, repr=False)
class ResponseResult:
    observation: Observation
    body: bytes | None = field(repr=False)
    completed_at: float


@dataclass(slots=True)
class _ScenarioMetrics:
    durations_ms: list[float] = field(default_factory=list)
    first_byte_ms: list[float] = field(default_factory=list)
    oracle_query_ms: list[float] = field(default_factory=list)
    oracle_pool_wait_ms: list[float] = field(default_factory=list)
    status_codes: Counter[int] = field(default_factory=Counter)
    error_kinds: Counter[str] = field(default_factory=Counter)
    response_bytes: int = 0
    expected: int = 0
    telemetry_missing: int = 0
    telemetry_invalid: int = 0
    authorization_outcomes: Counter[str] = field(default_factory=Counter)

    def add(self, observation: Observation) -> None:
        self.durations_ms.append(observation.duration_ms)
        if observation.first_byte_ms is not None:
            self.first_byte_ms.append(observation.first_byte_ms)
        self.response_bytes += observation.response_bytes
        self.expected += int(observation.expected)
        if observation.status_code is not None:
            self.status_codes[observation.status_code] += 1
        if observation.error_kind is not None:
            self.error_kinds[_safe_error_kind(observation.error_kind) or "internal"] += 1
        if observation.oracle_query_ms is None or observation.oracle_pool_wait_ms is None:
            self.telemetry_missing += 1
        if observation.oracle_query_ms is not None:
            self.oracle_query_ms.append(observation.oracle_query_ms)
        if observation.oracle_pool_wait_ms is not None:
            self.oracle_pool_wait_ms.append(observation.oracle_pool_wait_ms)
        self.telemetry_invalid += int(observation.telemetry_invalid)
        self.authorization_outcomes[
            _authorization_outcome(observation.scenario, observation.status_code)
        ] += 1

    def summary(self) -> dict[str, object]:
        request_count = len(self.durations_ms)
        errors = request_count - self.expected
        return {
            "requests": request_count,
            "expected_responses": self.expected,
            "errors": errors,
            "error_rate": round(errors / request_count, 6) if request_count else 0.0,
            "response_bytes": self.response_bytes,
            "status_codes": {str(key): self.status_codes[key] for key in sorted(self.status_codes)},
            "error_kinds": {key: self.error_kinds[key] for key in sorted(self.error_kinds)},
            "latency_ms": percentile_summary(self.durations_ms),
            "first_byte_ms": percentile_summary(self.first_byte_ms),
            "oracle_query_ms": percentile_summary(self.oracle_query_ms),
            "oracle_pool_wait_ms": percentile_summary(self.oracle_pool_wait_ms),
            "telemetry_missing_requests": self.telemetry_missing,
            "telemetry_invalid_requests": self.telemetry_invalid,
            "authorization_outcomes": {
                key: self.authorization_outcomes[key]
                for key in ("allowed", "denied", "not_applicable")
                if self.authorization_outcomes[key]
            },
        }


class ObservationRecorder:
    """Stream redacted events while retaining only numeric series for summaries."""

    def __init__(self, output_path: Path, *, seed: int, profile_digest: str) -> None:
        if not output_path.parent.is_dir():
            raise ProfileError("the events output parent directory must already exist")
        try:
            self._stream = output_path.open("x", encoding="utf-8", buffering=1)
        except FileExistsError as error:
            raise ProfileError("the events output file already exists") from error
        except OSError as error:
            raise ProfileError("the events output file could not be created") from error
        self._lock = threading.Lock()
        self._metrics: defaultdict[str, _ScenarioMetrics] = defaultdict(_ScenarioMetrics)
        self._started = time.monotonic()
        self._write(
            {
                "record_type": "run",
                "notice": UNVERIFIED_NOTICE,
                "seed": seed,
                "profile_sha256": profile_digest,
                "started_at": datetime.now(UTC).isoformat(),
            }
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stream.close()

    def record(self, observation: Observation) -> None:
        with self._lock:
            self._metrics[_safe_scenario(observation.scenario)].add(observation)
            self._write(observation.safe_mapping())

    def finish(self, *, seed: int) -> dict[str, object]:
        with self._lock:
            scenario_summaries = {
                name: self._metrics[name].summary() for name in sorted(self._metrics)
            }
            request_count = sum(len(metric.durations_ms) for metric in self._metrics.values())
            error_count = sum(
                len(metric.durations_ms) - metric.expected for metric in self._metrics.values()
            )
            elapsed_seconds = time.monotonic() - self._started
            summary: dict[str, object] = {
                "record_type": "summary",
                "notice": UNVERIFIED_NOTICE,
                "capacity_targets_status": "UNVERIFIED",
                "seed": seed,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "requests": request_count,
                "errors": error_count,
                "error_rate": round(error_count / request_count, 6) if request_count else 0.0,
                "requests_per_second": round(request_count / elapsed_seconds, 3)
                if elapsed_seconds
                else 0.0,
                "response_bytes": sum(metric.response_bytes for metric in self._metrics.values()),
                "scenarios": scenario_summaries,
            }
            self._write(summary)
            return summary

    def _write(self, value: Mapping[str, object]) -> None:
        self._stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")))
        self._stream.write("\n")


class RequestIdFactory:
    """Generate reproducible, non-secret request IDs from the retained seed."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._counter = 0
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            self._counter += 1
            counter = self._counter
        return str(uuid5(NAMESPACE_URL, f"agora-capacity:{self._seed}:{counter}"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: HTTPMessage,
        new_url: str,
    ) -> urllib.request.Request | None:
        return None


class HttpSession:
    """One cookie-isolated, proxy-free HTTP session pinned to configured origins."""

    def __init__(
        self,
        profile: CapacityProfile,
        recorder: ObservationRecorder,
        request_ids: RequestIdFactory,
    ) -> None:
        self._profile = profile
        self._recorder = recorder
        self._request_ids = request_ids
        cookie_jar = http.cookiejar.CookieJar()
        context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(cookie_jar),
            _NoRedirect(),
        )

    def request(self, spec: RequestSpec) -> ResponseResult:
        origin = (
            self._profile.portal_origin if spec.origin == "portal" else self._profile.content_origin
        )
        request_id = self._request_ids.next()
        data = None
        headers = {
            "Accept": "text/html,application/xhtml+xml" if spec.capture_body else "*/*",
            "User-Agent": "AgoraCapacityHarness/1",
            "X-Request-ID": request_id,
        }
        if spec.form is not None:
            if spec.origin != "portal":
                raise ProfileError("form requests must remain on the configured portal origin")
            data = urllib.parse.urlencode(spec.form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            # urllib is not a browser and does not add the HTTPS form context Django's CSRF
            # middleware validates. Supply the exact pinned portal origin without weakening TLS
            # verification or the no-redirect boundary.
            headers["Origin"] = self._profile.portal_origin.value
            headers["Referer"] = self._profile.portal_origin.url(spec.path)
        request = urllib.request.Request(
            origin.url(spec.path),
            data=data,
            headers=headers,
            method=spec.method,
        )
        started = time.monotonic()
        try:
            try:
                response = self._opener.open(
                    request,
                    timeout=self._profile.workload.request_timeout_seconds,
                )
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status_code = int(response.status)
                body, response_bytes, first_byte_at = _consume_response(
                    response,
                    capture=spec.capture_body,
                )
                query_ms, query_invalid = parse_nonnegative_metric_header(
                    response.headers,
                    self._profile.telemetry.oracle_query_ms,
                )
                pool_ms, pool_invalid = parse_nonnegative_metric_header(
                    response.headers,
                    self._profile.telemetry.oracle_pool_wait_ms,
                )
            completed = time.monotonic()
            expected = status_code in spec.expected_statuses
            observation = Observation(
                request_id=request_id,
                scenario=spec.scenario,
                status_code=status_code,
                duration_ms=(completed - started) * 1000,
                response_bytes=response_bytes,
                expected=expected,
                first_byte_ms=(first_byte_at - started) * 1000
                if first_byte_at is not None
                else None,
                oracle_query_ms=query_ms,
                oracle_pool_wait_ms=pool_ms,
                telemetry_invalid=query_invalid or pool_invalid,
                error_kind=None if expected else "unexpected_status",
            )
        except (ResponseTooLarge, OSError) as error:
            completed = time.monotonic()
            body = None
            observation = _failed_observation(
                request_id,
                spec.scenario,
                started,
                completed,
                classify_transport_error(error),
            )
        self._recorder.record(observation)
        return ResponseResult(observation=observation, body=body, completed_at=completed)

    def record_shape_failure(self, scenario: str) -> None:
        self._recorder.record(
            Observation(
                request_id=self._request_ids.next(),
                scenario=scenario,
                status_code=None,
                duration_ms=0.0,
                response_bytes=0,
                expected=False,
                error_kind="response_shape",
            )
        )

    def record_derived(self, *, scenario: str, status_code: int, duration_ms: float) -> None:
        self._recorder.record(
            Observation(
                request_id=self._request_ids.next(),
                scenario=scenario,
                status_code=status_code,
                duration_ms=duration_ms,
                response_bytes=0,
                expected=True,
            )
        )


class _PortalParser(HTMLParser):
    """Retain only narrow control values; never retain portal or artifact markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csrf_tokens: list[str] = []
        self.iframe_sources: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "csrfmiddlewaretoken":
            value = attributes.get("value")
            if value is not None:
                self.csrf_tokens.append(value)
        elif tag == "iframe":
            source = attributes.get("src")
            if source is not None:
                self.iframe_sources.append(source)
        elif tag == "a":
            href = attributes.get("href")
            if href is not None:
                self.links.append(href)


def parse_profile_document(
    document: bytes,
    *,
    allow_http_loopback: bool = False,
) -> CapacityProfile:
    """Parse and validate a profile without resolving secret environment values."""
    digest = hashlib.sha256(document).hexdigest()
    try:
        value = json.loads(document)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProfileError("the profile must be valid UTF-8 JSON") from error
    root = _mapping(value, "profile")
    _only_keys(
        root,
        {
            "schema_version",
            "capacity_targets_status",
            "portal_origin",
            "content_origin",
            "dashboard_id",
            "csv_logical_name",
            "owner",
            "viewer",
            "workload",
            "telemetry_headers",
        },
        "profile",
    )
    if root.get("schema_version") != 1:
        raise ProfileError("profile.schema_version must be 1")
    if root.get("capacity_targets_status") != "UNVERIFIED":
        raise ProfileError("profile.capacity_targets_status must be UNVERIFIED")
    portal_origin = parse_origin(
        _string(root, "portal_origin"),
        field_name="profile.portal_origin",
        allow_http_loopback=allow_http_loopback,
    )
    content_origin = parse_origin(
        _string(root, "content_origin"),
        field_name="profile.content_origin",
        allow_http_loopback=allow_http_loopback,
    )
    if portal_origin.hostname == content_origin.hostname:
        raise ProfileError("portal and content origins must use different hostnames")
    try:
        dashboard_id = UUID(_string(root, "dashboard_id"))
    except ValueError as error:
        raise ProfileError("profile.dashboard_id must be a UUID") from error
    csv_name = _string(root, "csv_logical_name")
    if _CSV_NAME.fullmatch(csv_name) is None:
        raise ProfileError("profile.csv_logical_name must be a narrow basename ending in .csv")
    return CapacityProfile(
        portal_origin=portal_origin,
        content_origin=content_origin,
        dashboard_id=dashboard_id,
        csv_logical_name=csv_name,
        owner=_credential_reference(root.get("owner"), "profile.owner"),
        viewer=_credential_reference(root.get("viewer"), "profile.viewer"),
        workload=_workload(root.get("workload")),
        telemetry=_telemetry(root.get("telemetry_headers")),
        source_digest=digest,
    )


def load_profile(path: Path, *, allow_http_loopback: bool = False) -> CapacityProfile:
    try:
        document = path.read_bytes()
    except OSError as error:
        raise ProfileError("the profile file could not be read") from error
    return parse_profile_document(document, allow_http_loopback=allow_http_loopback)


def parse_origin(value: str, *, field_name: str, allow_http_loopback: bool) -> Origin:
    if value != value.strip():
        raise ProfileError(f"{field_name} cannot contain surrounding whitespace")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ProfileError(f"{field_name} must be a valid origin") from error
    hostname = parsed.hostname
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProfileError(f"{field_name} must contain only scheme, host, and optional port")
    hostname = hostname.lower()
    if parsed.scheme == "http":
        if not allow_http_loopback or hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ProfileError(f"{field_name} must use HTTPS")
    elif parsed.scheme != "https":
        raise ProfileError(f"{field_name} must use HTTPS")
    host_display = f"[{hostname}]" if ":" in hostname else hostname
    port_display = f":{port}" if port is not None else ""
    normalized = f"{parsed.scheme}://{host_display}{port_display}"
    return Origin(normalized, parsed.scheme, hostname, port)


def resolve_credentials(
    profile: CapacityProfile,
    environ: Mapping[str, str],
) -> tuple[Credentials, Credentials]:
    owner = _resolve_credential(profile.owner, environ, "owner")
    viewer = _resolve_credential(profile.viewer, environ, "viewer")
    if owner.soeid == viewer.soeid:
        raise ProfileError("owner and viewer identities must be different")
    return owner, viewer


def build_static_request_shapes(profile: CapacityProfile) -> dict[str, RequestSpec]:
    """Expose deterministic safe request shapes for review and unit verification."""
    dashboard = str(profile.dashboard_id)
    return {
        "shared_with_me": RequestSpec(
            scenario="shared_with_me",
            origin="portal",
            method="GET",
            path="/projects/?scope=shared",
            expected_statuses=frozenset({200}),
        ),
        "grant_check": RequestSpec(
            scenario="grant_check",
            origin="portal",
            method="GET",
            path=f"/projects/{dashboard}/",
            expected_statuses=frozenset({200}),
        ),
        "render_start": RequestSpec(
            scenario="render_start",
            origin="portal",
            method="GET",
            path=f"/projects/{dashboard}/view/",
            expected_statuses=frozenset({200}),
            capture_body=True,
        ),
    }


def percentile_summary(values: Sequence[float]) -> dict[str, float | int | None]:
    """Return deterministic nearest-rank percentiles for a numeric observation series."""
    if not values:
        return {"samples": 0, "p50": None, "p95": None, "p99": None}
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "p50": _rounded_or_none(_nearest_rank(ordered, 0.50)),
        "p95": _rounded_or_none(_nearest_rank(ordered, 0.95)),
        "p99": _rounded_or_none(_nearest_rank(ordered, 0.99)),
    }


def summarize_observations(observations: Sequence[Observation]) -> dict[str, dict[str, object]]:
    """Summarize an in-memory deterministic sample for unit checks and small diagnostics."""
    metrics: defaultdict[str, _ScenarioMetrics] = defaultdict(_ScenarioMetrics)
    for observation in observations:
        metrics[_safe_scenario(observation.scenario)].add(observation)
    return {name: metrics[name].summary() for name in sorted(metrics)}


def parse_nonnegative_metric_header(
    headers: Mapping[str, str],
    name: str,
) -> tuple[float | None, bool]:
    raw = headers.get(name)
    if raw is None:
        return None, False
    try:
        value = float(raw)
    except ValueError:
        return None, True
    if not math.isfinite(value) or value < 0:
        return None, True
    return value, False


def classify_transport_error(error: BaseException) -> str:
    """Reduce arbitrary exceptions to an allowlisted label without formatting them."""
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ResponseTooLarge):
        return "response_too_large"
    return "network"


def run_capacity(
    profile: CapacityProfile,
    *,
    owner: Credentials,
    viewer: Credentials,
    output_path: Path,
    allow_mutations: bool,
) -> dict[str, object]:
    if profile.workload.mutation_cycles and not allow_mutations:
        raise ProfileError("mutation_cycles requires the explicit --allow-mutations flag")
    request_ids = RequestIdFactory(profile.workload.seed)
    with ObservationRecorder(
        output_path,
        seed=profile.workload.seed,
        profile_digest=profile.source_digest,
    ) as recorder:
        started = time.monotonic()
        deadline = started + profile.workload.ramp_seconds + profile.workload.hold_seconds
        with ThreadPoolExecutor(
            max_workers=profile.workload.virtual_users,
            thread_name_prefix="agora-capacity",
        ) as executor:
            futures = [
                executor.submit(
                    _read_worker,
                    index,
                    profile,
                    viewer,
                    recorder,
                    request_ids,
                    started,
                    deadline,
                )
                for index in range(profile.workload.virtual_users)
            ]
            for future in futures:
                try:
                    future.result()
                except ScenarioFailure:
                    # The precise request or safe response-shape failure is already recorded.
                    pass
        if profile.workload.mutation_cycles:
            try:
                _run_mutations(profile, owner, viewer, recorder, request_ids)
            except ScenarioFailure:
                pass
        return recorder.finish(seed=profile.workload.seed)


def _read_worker(
    index: int,
    profile: CapacityProfile,
    credentials: Credentials,
    recorder: ObservationRecorder,
    request_ids: RequestIdFactory,
    started: float,
    deadline: float,
) -> None:
    if profile.workload.ramp_seconds:
        delay = profile.workload.ramp_seconds * index / profile.workload.virtual_users
        time.sleep(max(0.0, started + delay - time.monotonic()))
    session = HttpSession(profile, recorder, request_ids)
    _login(session, credentials)
    randomizer = random.Random(profile.workload.seed + index)
    scenarios = tuple(sorted(profile.workload.read_weights))
    weights = tuple(profile.workload.read_weights[name] for name in scenarios)
    shapes = build_static_request_shapes(profile)
    while time.monotonic() < deadline:
        scenario = randomizer.choices(scenarios, weights=weights, k=1)[0]
        if scenario == "render_delivery":
            _render_delivery(session, profile, shapes["render_start"])
        else:
            result = session.request(shapes[scenario])
            _require_expected(result)
        if profile.workload.think_time_ms:
            maximum = profile.workload.think_time_ms * 2 / 1000
            time.sleep(randomizer.uniform(0, maximum))


def _login(session: HttpSession, credentials: Credentials) -> None:
    form_page = session.request(
        RequestSpec(
            scenario="signed_in_session_csrf",
            origin="portal",
            method="GET",
            path="/login/",
            expected_statuses=frozenset({200}),
            capture_body=True,
        )
    )
    _require_expected(form_page)
    token = extract_csrf_token(form_page.body)
    result = session.request(
        RequestSpec(
            scenario="signed_in_session",
            origin="portal",
            method="POST",
            path="/login/",
            expected_statuses=frozenset({302}),
            form={
                "csrfmiddlewaretoken": token,
                "soeid": credentials.soeid,
                "password": credentials.password,
                "next": "/",
            },
        )
    )
    _require_expected(result)


def _render_delivery(
    session: HttpSession,
    profile: CapacityProfile,
    render_start: RequestSpec,
) -> None:
    shell = session.request(render_start)
    _require_expected(shell)
    try:
        html_path = _extract_render_path(shell.body, profile.content_origin)
    except ScenarioFailure:
        session.record_shape_failure("render_start_shape")
        raise
    html = session.request(
        RequestSpec(
            scenario="html_delivery",
            origin="content",
            method="GET",
            path=html_path,
            expected_statuses=frozenset({200}),
        )
    )
    _require_expected(html)
    csv_path = f"{html_path}{urllib.parse.quote(profile.csv_logical_name, safe='._-')}"
    csv = session.request(
        RequestSpec(
            scenario="csv_delivery",
            origin="content",
            method="GET",
            path=csv_path,
            expected_statuses=frozenset({200}),
        )
    )
    _require_expected(csv)


def _run_mutations(
    profile: CapacityProfile,
    owner: Credentials,
    viewer: Credentials,
    recorder: ObservationRecorder,
    request_ids: RequestIdFactory,
) -> None:
    owner_session = HttpSession(profile, recorder, request_ids)
    viewer_session = HttpSession(profile, recorder, request_ids)
    _login(owner_session, owner)
    _login(viewer_session, viewer)
    for _ in range(profile.workload.mutation_cycles):
        try:
            _mutation_cycle(profile, owner_session, viewer_session, viewer)
        except ScenarioFailure:
            break


def _mutation_cycle(
    profile: CapacityProfile,
    owner_session: HttpSession,
    viewer_session: HttpSession,
    viewer: Credentials,
) -> None:
    access_path = f"/projects/{profile.dashboard_id}/access/"
    access_page = owner_session.request(
        RequestSpec(
            scenario="revoke_prepare",
            origin="portal",
            method="GET",
            path=access_path,
            expected_statuses=frozenset({200}),
            capture_body=True,
        )
    )
    _require_expected(access_page)
    try:
        revoke_path = _extract_revoke_path(access_page.body, profile.portal_origin)
    except ScenarioFailure:
        owner_session.record_shape_failure("revoke_prepare_shape")
        raise
    confirmation = owner_session.request(
        RequestSpec(
            scenario="revoke_prepare",
            origin="portal",
            method="GET",
            path=revoke_path,
            expected_statuses=frozenset({200}),
            capture_body=True,
        )
    )
    _require_expected(confirmation)
    csrf = extract_csrf_token(confirmation.body)
    revoked = owner_session.request(
        RequestSpec(
            scenario="revoke_mutation",
            origin="portal",
            method="POST",
            path=revoke_path,
            expected_statuses=frozenset({302}),
            form={"csrfmiddlewaretoken": csrf, "confirm": "on"},
        )
    )
    _require_expected(revoked)
    _wait_for_access_status(
        profile,
        viewer_session,
        wanted_status=404,
        started=revoked.completed_at,
        scenario="revocation_propagation",
    )
    grant_page = owner_session.request(
        RequestSpec(
            scenario="grant_prepare",
            origin="portal",
            method="GET",
            path=access_path,
            expected_statuses=frozenset({200}),
            capture_body=True,
        )
    )
    _require_expected(grant_page)
    csrf = extract_csrf_token(grant_page.body)
    granted = owner_session.request(
        RequestSpec(
            scenario="grant_mutation",
            origin="portal",
            method="POST",
            path=access_path,
            expected_statuses=frozenset({302}),
            form={"csrfmiddlewaretoken": csrf, "soeid": viewer.soeid},
        )
    )
    _require_expected(granted)
    _wait_for_access_status(
        profile,
        viewer_session,
        wanted_status=200,
        started=granted.completed_at,
        scenario="grant_propagation",
    )


def _wait_for_access_status(
    profile: CapacityProfile,
    session: HttpSession,
    *,
    wanted_status: int,
    started: float,
    scenario: str,
) -> None:
    deadline = started + profile.workload.propagation_timeout_seconds
    path = f"/projects/{profile.dashboard_id}/"
    while True:
        check = session.request(
            RequestSpec(
                scenario="authorization_check",
                origin="portal",
                method="GET",
                path=path,
                expected_statuses=frozenset({200, 404}),
            )
        )
        if check.observation.status_code == wanted_status:
            session.record_derived(
                scenario=scenario,
                status_code=wanted_status,
                duration_ms=(check.completed_at - started) * 1000,
            )
            return
        if check.observation.status_code not in {200, 404} or time.monotonic() >= deadline:
            session.record_shape_failure(scenario)
            raise ScenarioFailure("response_shape")
        time.sleep(profile.workload.propagation_poll_ms / 1000)


def extract_csrf_token(body: bytes | None) -> str:
    """Select one syntactically valid masked token from a page with several POST forms."""
    parser = _parse_portal(body)
    for token in parser.csrf_tokens:
        if _CSRF_TOKEN.fullmatch(token) is not None:
            return token
    raise ScenarioFailure("response_shape")


def _extract_render_path(body: bytes | None, content_origin: Origin) -> str:
    parser = _parse_portal(body)
    if len(parser.iframe_sources) != 1:
        raise ScenarioFailure("response_shape")
    path = content_origin.checked_path(parser.iframe_sources[0])
    if _RENDER_PATH.fullmatch(path) is None:
        raise ScenarioFailure("response_shape")
    return path


def _extract_revoke_path(body: bytes | None, portal_origin: Origin) -> str:
    parser = _parse_portal(body)
    candidates = []
    for link in parser.links:
        try:
            path = portal_origin.checked_path(link)
        except ScenarioFailure:
            continue
        if _REVOKE_PATH.fullmatch(path) is not None:
            candidates.append(path)
    if len(candidates) != 1:
        raise ScenarioFailure("response_shape")
    return candidates[0]


def _parse_portal(body: bytes | None) -> _PortalParser:
    if body is None:
        raise ScenarioFailure("response_shape")
    try:
        markup = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ScenarioFailure("response_shape") from error
    parser = _PortalParser()
    parser.feed(markup)
    parser.close()
    return parser


def _consume_response(
    response: _ReadableResponse,
    *,
    capture: bool,
) -> tuple[bytes | None, int, float | None]:
    body = bytearray() if capture else None
    byte_count = 0
    first_byte_at: float | None = None
    first_byte = response.read(1)
    if first_byte:
        first_byte_at = time.monotonic()
        byte_count = 1
        if body is not None:
            body.extend(first_byte)
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        byte_count += len(chunk)
        if body is not None:
            if byte_count > _CAPTURE_LIMIT:
                raise ResponseTooLarge
            body.extend(chunk)
    return (bytes(body) if body is not None else None), byte_count, first_byte_at


def _failed_observation(
    request_id: str,
    scenario: str,
    started: float,
    completed: float,
    error_kind: str,
) -> Observation:
    if error_kind not in _SAFE_ERROR_KINDS:
        error_kind = "network"
    return Observation(
        request_id=request_id,
        scenario=scenario,
        status_code=None,
        duration_ms=(completed - started) * 1000,
        response_bytes=0,
        expected=False,
        error_kind=error_kind,
    )


def _require_expected(result: ResponseResult) -> None:
    if not result.observation.expected:
        raise ScenarioFailure(result.observation.error_kind or "unexpected_status")


def _nearest_rank(ordered: Sequence[float], percentile: float) -> float:
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _rounded_or_none(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _safe_request_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        return "invalid-request-id"


def _safe_scenario(value: str) -> str:
    return value if value in _SAFE_SCENARIOS else "harness_internal"


def _safe_error_kind(value: str | None) -> str | None:
    if value is None:
        return None
    return value if value in _SAFE_ERROR_KINDS else "internal"


def _authorization_outcome(scenario: str, status_code: int | None) -> str:
    if scenario not in _AUTHORIZATION_SCENARIOS:
        return "not_applicable"
    if status_code == 200:
        return "allowed"
    if status_code in {403, 404}:
        return "denied"
    return "not_applicable"


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProfileError(f"{field_name} must be an object")
    return value


def _only_keys(value: Mapping[str, object], allowed: set[str], field_name: str) -> None:
    if set(value) - allowed:
        raise ProfileError(f"{field_name} contains unknown keys")


def _string(value: Mapping[str, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate:
        raise ProfileError(f"profile.{key} must be a non-empty string")
    return candidate


def _credential_reference(value: object, field_name: str) -> CredentialReference:
    mapping = _mapping(value, field_name)
    _only_keys(mapping, {"soeid_env", "password_env"}, field_name)
    soeid_env = mapping.get("soeid_env")
    password_env = mapping.get("password_env")
    if not isinstance(soeid_env, str) or _ENV_NAME.fullmatch(soeid_env) is None:
        raise ProfileError(f"{field_name}.soeid_env must be an uppercase environment name")
    if not isinstance(password_env, str) or _ENV_NAME.fullmatch(password_env) is None:
        raise ProfileError(f"{field_name}.password_env must be an uppercase environment name")
    if soeid_env == password_env:
        raise ProfileError(f"{field_name} must use different identity and password variables")
    return CredentialReference(soeid_env=soeid_env, password_env=password_env)


def _resolve_credential(
    reference: CredentialReference,
    environ: Mapping[str, str],
    role: str,
) -> Credentials:
    soeid = environ.get(reference.soeid_env)
    password = environ.get(reference.password_env)
    if soeid is None or not soeid.strip() or soeid != soeid.strip():
        raise ProfileError(f"the {role} identity environment variable is missing or blank")
    if password is None or not password:
        raise ProfileError(f"the {role} password environment variable is missing or blank")
    return Credentials(soeid=soeid, password=password)


def _workload(value: object) -> Workload:
    mapping = _mapping(value, "profile.workload")
    keys = {
        "virtual_users",
        "ramp_seconds",
        "hold_seconds",
        "mutation_cycles",
        "request_timeout_seconds",
        "propagation_timeout_seconds",
        "propagation_poll_ms",
        "think_time_ms",
        "seed",
        "read_weights",
    }
    _only_keys(mapping, keys, "profile.workload")
    read_weights_value = _mapping(mapping.get("read_weights"), "profile.workload.read_weights")
    _only_keys(read_weights_value, set(_READ_SCENARIOS), "profile.workload.read_weights")
    if set(read_weights_value) != _READ_SCENARIOS:
        raise ProfileError("profile.workload.read_weights must name every read scenario")
    read_weights = {
        name: _bounded_int(read_weights_value, name, minimum=0, maximum=1000)
        for name in sorted(_READ_SCENARIOS)
    }
    if not any(read_weights.values()):
        raise ProfileError("at least one read scenario weight must be positive")
    return Workload(
        virtual_users=_bounded_int(mapping, "virtual_users", minimum=1, maximum=10_000),
        ramp_seconds=_bounded_number(mapping, "ramp_seconds", minimum=0, maximum=86_400),
        hold_seconds=_bounded_number(mapping, "hold_seconds", minimum=0.1, maximum=86_400),
        mutation_cycles=_bounded_int(mapping, "mutation_cycles", minimum=0, maximum=100_000),
        request_timeout_seconds=_bounded_number(
            mapping,
            "request_timeout_seconds",
            minimum=0.1,
            maximum=300,
        ),
        propagation_timeout_seconds=_bounded_number(
            mapping,
            "propagation_timeout_seconds",
            minimum=0.1,
            maximum=300,
        ),
        propagation_poll_ms=_bounded_int(
            mapping,
            "propagation_poll_ms",
            minimum=10,
            maximum=60_000,
        ),
        think_time_ms=_bounded_int(mapping, "think_time_ms", minimum=0, maximum=60_000),
        seed=_bounded_int(mapping, "seed", minimum=0, maximum=2**63 - 1),
        read_weights=read_weights,
    )


def _telemetry(value: object) -> TelemetryHeaders:
    mapping = _mapping(value, "profile.telemetry_headers")
    _only_keys(
        mapping,
        {"oracle_query_ms", "oracle_pool_wait_ms"},
        "profile.telemetry_headers",
    )
    names: dict[str, str] = {}
    for key in ("oracle_query_ms", "oracle_pool_wait_ms"):
        candidate = mapping.get(key)
        if not isinstance(candidate, str) or _HEADER_NAME.fullmatch(candidate) is None:
            raise ProfileError(f"profile.telemetry_headers.{key} must be an HTTP header name")
        names[key] = candidate
    if names["oracle_query_ms"].lower() == names["oracle_pool_wait_ms"].lower():
        raise ProfileError("Oracle query and pool wait telemetry must use different headers")
    return TelemetryHeaders(**names)


def _bounded_int(
    mapping: Mapping[str, object],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ProfileError(f"profile.workload.{key} must be between {minimum} and {maximum}")
    return value


def _bounded_number(
    mapping: Mapping[str, object],
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    value = mapping.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProfileError(f"profile.workload.{key} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ProfileError(f"profile.workload.{key} must be between {minimum} and {maximum}")
    return normalized


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", required=True, type=Path, help="Path to a version 1 JSON profile"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform network requests; omission performs validation only",
    )
    parser.add_argument(
        "--allow-mutations",
        action="store_true",
        help="Permit the dedicated fixture's configured grant/revoke cycles",
    )
    parser.add_argument(
        "--events-output",
        type=Path,
        help="New JSONL file for redacted request observations (required with --execute)",
    )
    parser.add_argument(
        "--allow-http-loopback",
        action="store_true",
        help="Permit HTTP only for localhost/loopback smoke validation",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        profile = load_profile(
            arguments.profile,
            allow_http_loopback=arguments.allow_http_loopback,
        )
        if not arguments.execute:
            print(
                json.dumps(
                    {
                        "capacity_targets_status": "UNVERIFIED",
                        "notice": UNVERIFIED_NOTICE,
                        "profile_sha256": profile.source_digest,
                        "validation": "ok",
                    },
                    sort_keys=True,
                )
            )
            return 0
        if arguments.events_output is None:
            raise ProfileError("--events-output is required with --execute")
        owner, viewer = resolve_credentials(profile, os.environ)
        summary = run_capacity(
            profile,
            owner=owner,
            viewer=viewer,
            output_path=arguments.events_output,
            allow_mutations=arguments.allow_mutations,
        )
    except ProfileError as error:
        print(f"Capacity profile error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Capacity run interrupted; inspect the redacted event file for partial results.")
        return 130
    except Exception:
        print(
            "Capacity run failed with an internal error; request details were not emitted.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(summary, sort_keys=True))
    error_count = summary.get("errors")
    return 1 if isinstance(error_count, int) and error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
