"""Dependency-light validation and bounded staging for dashboard-package uploads.

This module deliberately knows nothing about HTTP multipart parsing, authentication, or
publication.  It accepts a typed manifest of one-shot byte iterables, validates the manifest and
content, and stages unchanged bytes so a persistence adapter can commit one complete revision.
Uploaded HTML and package assets remain hostile content; validation here is only a format and
dependency policy check, not a browser security boundary.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from types import MappingProxyType
from typing import BinaryIO, Final, NoReturn, cast
from urllib.parse import unquote_to_bytes, urlsplit

from agora.core.names import InvalidLogicalName, LogicalName, normalize_logical_name


class UploadKind(StrEnum):
    """The only artifact kinds accepted by the upload boundary.

    IMAGE and FONT are deliberately broad persistence kinds.  Their concrete MIME subtype is
    carried separately on :class:`StagedUploadFile`; this avoids making a persistence adapter
    guess whether an IMAGE is PNG, JPEG, GIF, or WebP (and likewise for WOFF versus WOFF2).
    Subtype aliases are retained for callers that want a readable filename-oriented comparison.
    """

    HTML = "html"
    CSV = "csv"
    CSS = "css"
    IMAGE = "img"
    FONT = "font"
    PNG = IMAGE
    JPEG = IMAGE
    JPG = IMAGE
    GIF = IMAGE
    WEBP = IMAGE
    WOFF = FONT
    WOFF2 = FONT


# Keep these maps immutable.  In particular, no request-provided media type participates in
# classification: a filename's supported extension determines both the kind and the canonical
# media type carried to persistence.
UPLOAD_EXTENSION_TO_KIND: Final[Mapping[str, UploadKind]] = MappingProxyType(
    {
        "html": UploadKind.HTML,
        "csv": UploadKind.CSV,
        "css": UploadKind.CSS,
        "png": UploadKind.IMAGE,
        "jpg": UploadKind.IMAGE,
        "jpeg": UploadKind.IMAGE,
        "gif": UploadKind.IMAGE,
        "webp": UploadKind.IMAGE,
        "woff": UploadKind.FONT,
        "woff2": UploadKind.FONT,
    }
)
UPLOAD_KIND_TO_MEDIA_TYPES: Final[Mapping[UploadKind, frozenset[str]]] = MappingProxyType(
    {
        UploadKind.HTML: frozenset({"text/html"}),
        UploadKind.CSV: frozenset({"text/csv"}),
        UploadKind.CSS: frozenset({"text/css"}),
        UploadKind.IMAGE: frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"}),
        UploadKind.FONT: frozenset({"font/woff", "font/woff2"}),
    }
)
UPLOAD_EXTENSION_TO_MEDIA_TYPE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "html": "text/html",
        "csv": "text/csv",
        "css": "text/css",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
        "woff": "font/woff",
        "woff2": "font/woff2",
    }
)
UPLOAD_EXTENSION_MAP: Final[Mapping[str, tuple[UploadKind, str]]] = MappingProxyType(
    {
        extension: (UPLOAD_EXTENSION_TO_KIND[extension], media_type)
        for extension, media_type in UPLOAD_EXTENSION_TO_MEDIA_TYPE.items()
    }
)
# The singular name is retained for unambiguous kinds.  For IMAGE/FONT callers should use the
# extension table above because a broad kind has more than one valid subtype.
UPLOAD_KIND_TO_MEDIA_TYPE: Final[Mapping[UploadKind, str]] = MappingProxyType(
    {
        UploadKind.HTML: "text/html",
        UploadKind.CSV: "text/csv",
        UploadKind.CSS: "text/css",
    }
)

# Short aliases make the tables discoverable to adapters without creating a second mutable
# source of truth.  They intentionally point at the exact same mapping objects.
EXTENSION_TO_KIND = UPLOAD_EXTENSION_TO_KIND
KIND_TO_MEDIA_TYPE = UPLOAD_KIND_TO_MEDIA_TYPE
KIND_TO_MEDIA_TYPES = UPLOAD_KIND_TO_MEDIA_TYPES
EXTENSION_TO_MEDIA_TYPE = UPLOAD_EXTENSION_TO_MEDIA_TYPE
MEDIA_TYPE_BY_EXTENSION = UPLOAD_EXTENSION_TO_MEDIA_TYPE


class UploadIssueCode(StrEnum):
    """Stable, non-content-bearing reasons for rejecting an upload."""

    MALFORMED_MULTIPART = "malformed_multipart"
    TOO_MANY_FILES = "too_many_files"
    MISSING_HTML = "missing_html"
    MULTIPLE_HTML = "multiple_html"
    INVALID_FILENAME = "invalid_filename"
    DUPLICATE_FILENAME = "duplicate_filename"
    EXTENSION_MISMATCH = "extension_mismatch"
    MEDIA_TYPE_MISMATCH = "media_type_mismatch"
    EMPTY_FILE = "empty_file"
    FILE_TOO_LARGE = "file_too_large"
    TOTAL_TOO_LARGE = "total_too_large"
    TOO_MANY_CHUNKS = "too_many_chunks"
    INVALID_UTF8 = "invalid_utf8"
    BINARY_CONTENT = "binary_content"
    CSV_MALFORMED = "csv_malformed"
    CSV_TOO_MANY_ROWS = "csv_too_many_rows"
    CSV_TOO_MANY_FIELDS = "csv_too_many_fields"
    CSV_FIELD_TOO_LARGE = "csv_field_too_large"
    CSV_RECORD_TOO_LARGE = "csv_record_too_large"
    HTML_MALFORMED = "html_malformed"
    HTML_TOO_COMPLEX = "html_too_complex"
    EXTERNAL_DEPENDENCY = "external_dependency"
    INVALID_ASSET_REFERENCE = "invalid_asset_reference"
    MISSING_ASSET_REFERENCE = "missing_asset_reference"
    # Compatibility aliases for callers that use the more formal artifact wording.
    INVALID_ARTIFACT_REFERENCE = INVALID_ASSET_REFERENCE
    MISSING_ARTIFACT_REFERENCE = MISSING_ASSET_REFERENCE
    INVALID_CSV_REFERENCE = "invalid_csv_reference"
    MISSING_CSV_REFERENCE = "missing_csv_reference"
    NON_BYTES_CHUNK = "non_bytes_chunk"
    STREAM_ERROR = "stream_error"
    STAGING_ERROR = "staging_error"


class UploadIssueField(StrEnum):
    """Safe structural locations for actionable upload diagnostics.

    Values are application-owned constants: they never contain uploaded filenames, URLs, or
    parser excerpts, so callers may include them in logs without reflecting hostile content.
    """

    FILENAME = "filename"
    MEDIA_TYPE = "media_type"
    CSS_IMPORT = "css_import"
    CSS_EXTERNAL_URL = "css_external_url"
    HTML_PROCESSING_INSTRUCTION = "html_processing_instruction"
    HTML_EMBEDDED_CONTENT = "html_embedded_content"
    HTML_LINK = "html_link"
    HTML_SCRIPT_SOURCE = "html_script_source"
    HTML_MANIFEST = "html_manifest"
    HTML_FORM_ACTION = "html_form_action"
    HTML_RESPONSIVE_SOURCE = "html_responsive_source"
    HTML_META_URL = "html_meta_url"
    HTML_META_REFRESH = "html_meta_refresh"
    INLINE_SCRIPT_URL = "inline_script_url"
    INLINE_NETWORK_API = "inline_network_api"
    DYNAMIC_DATA_REFERENCE = "dynamic_data_reference"
    DATA_TEMPLATE_LITERAL = "data_template_literal"
    DATA_IDENTIFIER = "data_identifier"
    DATA_EXPRESSION = "data_expression"
    EXTERNAL_REFERENCE = "external_reference"


@dataclass(frozen=True, slots=True)
class UploadIssue:
    """A safe, file-specific rejection description.

    The part index is intentionally used instead of echoing attacker-controlled filenames or
    parser details.  Callers may display a separately escaped normalized name after validation.
    """

    code: UploadIssueCode
    part_index: int | None = None
    field: UploadIssueField | None = None


class UploadRejected(ValueError):
    """Raised when an upload violates the typed upload contract."""

    def __init__(self, issue: UploadIssue) -> None:
        self.issue = issue
        self.issues = (issue,)
        location = ""
        if issue.part_index is not None:
            location = f" for part {issue.part_index}"
        field = f" ({issue.field.value})" if issue.field is not None else ""
        super().__init__(f"upload rejected{location}: {issue.code.value}{field}")


class UploadValidationError(UploadRejected):
    """Compatibility name for callers that distinguish validation from persistence errors."""


@dataclass(frozen=True, slots=True)
class UploadPart:
    """One untrusted logical part and its one-shot source of raw bytes.

    ``content_length`` is advisory metadata only.  It is deliberately never used as the byte
    count authority; actual yielded bytes are counted and hashed by :func:`stage_upload`.
    """

    filename: str | bytes
    chunks: Iterable[bytes] | Callable[[], Iterable[bytes]]
    media_type: str | None = None
    content_length: int | None = None

    @property
    def declared_media_type(self) -> str | None:
        """Expose the HTTP-facing spelling without making it a second source of truth."""

        return self.media_type


@dataclass(frozen=True, slots=True)
class UploadLimits:
    """Explicit application limits independent of edge or multipart ``Content-Length`` limits."""

    max_file_bytes: int = 25 * 1024 * 1024
    max_total_bytes: int = 100 * 1024 * 1024
    max_files: int = 51
    max_csv_rows: int = 100_000
    max_csv_fields: int = 1_024
    max_csv_field_bytes: int = 1 * 1024 * 1024
    max_csv_record_bytes: int = 4 * 1024 * 1024
    max_html_tokens: int = 100_000
    max_html_nesting: int = 256
    max_chunks_per_file: int = 100_000

    def __post_init__(self) -> None:
        if self.max_file_bytes < 0 or self.max_total_bytes < 0:
            raise ValueError("upload byte limits must be nonnegative")
        if self.max_files < 1:
            raise ValueError("upload file limit must include the HTML artifact")
        for name in (
            "max_csv_rows",
            "max_csv_fields",
            "max_csv_field_bytes",
            "max_csv_record_bytes",
            "max_html_tokens",
            "max_html_nesting",
            "max_chunks_per_file",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")

    @property
    def max_supporting_files(self) -> int:
        """Maximum supporting files implied by the total part limit."""

        return self.max_files - 1

    @property
    def max_csv_files(self) -> int:
        """Compatibility alias for the former CSV-only package limit."""

        return self.max_supporting_files

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, str], *, prefix: str = "AGORA_UPLOAD_"
    ) -> UploadLimits:
        """Build limits from decimal configuration values without embedding environment access."""

        fields = (
            "max_file_bytes",
            "max_total_bytes",
            "max_files",
            "max_csv_rows",
            "max_csv_fields",
            "max_csv_field_bytes",
            "max_csv_record_bytes",
            "max_html_tokens",
            "max_html_nesting",
            "max_chunks_per_file",
        )
        parsed: dict[str, int] = {}
        for field in fields:
            key = f"{prefix}{field.upper()}"
            raw = values.get(key)
            if raw is None:
                continue
            try:
                parsed[field] = int(raw, 10)
            except (TypeError, ValueError) as error:
                raise ValueError(f"{key} must be a decimal integer") from error
        return cls(**parsed)


@dataclass(frozen=True, slots=True)
class StagedUploadFile:
    """Validated metadata plus a private temporary file containing unchanged bytes."""

    kind: UploadKind
    logical_name: LogicalName
    byte_size: int
    sha256: str
    _stream: BinaryIO
    # This is derived from the authoritative extension map, never copied from the request.
    # A default preserves construction compatibility for a few low-level callers that create a
    # synthetic staged file solely to exercise replay/cleanup behavior.
    media_type: str = "application/octet-stream"

    @property
    def filename(self) -> str:
        return self.logical_name.display

    def iter_chunks(self, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        """Replay staged bytes as bounded chunks for the durable storage adapter."""

        if chunk_size < 1:
            raise ValueError("chunk size must be positive")
        try:
            self._stream.seek(0)
            while True:
                chunk = self._stream.read(chunk_size)
                if not isinstance(chunk, bytes):
                    raise TypeError("staged artifact did not return bytes")
                if not chunk:
                    break
                yield chunk
        except UploadRejected:
            raise
        except Exception as error:
            raise OSError("could not read staged upload") from error

    def close(self) -> None:
        self._stream.close()


@dataclass(slots=True, init=False)
class StagedUpload:
    """All validated parts held until the persistence service commits or rejects the revision."""

    files: tuple[StagedUploadFile, ...]
    total_size: int
    referenced_artifact_names: frozenset[str]
    _closed: bool = False

    def __init__(
        self,
        files: tuple[StagedUploadFile, ...],
        total_size: int,
        referenced_artifact_names: frozenset[str] = frozenset(),
        _closed: bool = False,
        *,
        referenced_csv_names: frozenset[str] | None = None,
    ) -> None:
        """Build a staged package, accepting the former CSV-only keyword as a compatibility aid."""

        references = frozenset(referenced_artifact_names)
        if referenced_csv_names is not None:
            references = references | frozenset(referenced_csv_names)
        self.files = files
        self.total_size = total_size
        self.referenced_artifact_names = references
        self._closed = _closed

    @property
    def referenced_csv_names(self) -> frozenset[str]:
        """Backward-compatible CSV-only view of all validated package references."""

        return frozenset(
            name for name in self.referenced_artifact_names if _CSV_SUFFIX_RE.search(name)
        )

    @property
    def artifacts(self) -> tuple[StagedUploadFile, ...]:
        """Alias that makes the staged manifest read naturally at the persistence boundary."""

        return self.files

    def close(self) -> None:
        if self._closed:
            return
        first_error: BaseException | None = None
        for file in self.files:
            try:
                file.close()
            except BaseException as error:  # pragma: no cover - unusual tempfile close failure
                if first_error is None:
                    first_error = error
        self._closed = True
        if first_error is not None:
            raise first_error

    def __enter__(self) -> StagedUpload:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        self.close()


ValidatedUpload = StagedUpload


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    index: int
    part: UploadPart
    kind: UploadKind
    name: LogicalName
    media_type: str


_CSV_FIELD_LIMIT_LOCK = threading.Lock()
_HTML_VOID_ELEMENTS: Final[frozenset[str]] = frozenset(
    {
        "area",
        "br",
        "col",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_HTML_FORBIDDEN_ELEMENTS: Final[frozenset[str]] = frozenset(
    {"applet", "base", "embed", "frame", "frameset", "iframe", "object", "portal"}
)
_URL_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "action",
        "background",
        "cite",
        "code",
        "data",
        "formaction",
        "href",
        "longdesc",
        "manifest",
        "ping",
        "poster",
        "profile",
        "src",
        "usemap",
        "xlink:href",
    }
)
_FORM_URL_ATTRIBUTES: Final[frozenset[str]] = frozenset({"action", "formaction"})
_SAFE_DATA_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"data:(?:image/(?:png|jpe?g|gif|webp)|font/woff2?)(?:[;,]|$)",
    re.IGNORECASE,
)
_EXTERNAL_URL_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:\b(?:https?|ftp|file):\s*/\s*/|//[A-Za-z0-9])"
)
_CSV_CALL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?is)\b(?:fetch|d3\s*\.\s*csv)\s*\(\s*(['\"])(?P<url>[^'\"]*)\1"
)
_CSV_TEMPLATE_CALL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?is)\b(?:fetch|d3\s*\.\s*csv)\s*\(\s*`(?P<url>[^`]*)`"
)
_D3_CSV_RE: Final[re.Pattern[str]] = re.compile(r"(?is)\bd3\s*\.\s*csv\s*\(")
_DYNAMIC_NETWORK_RE: Final[re.Pattern[str]] = re.compile(
    r"(?is)\b(?:XMLHttpRequest|WebSocket|EventSource|ImportScripts|sendBeacon|"
    r"Worker|SharedWorker)\s*\(|"
    r"navigator\s*\.\s*serviceWorker\b|"
    r"\bserviceWorker\s*(?:\.|\[)|"
    r"\bimport\s*\(|"
    r"(?:^|[;{}\n])\s*import\s+(?:['\"{*A-Za-z_$])"
)
_FETCH_RE: Final[re.Pattern[str]] = re.compile(r"(?is)\bfetch\s*\(")
_CSS_URL_RE: Final[re.Pattern[str]] = re.compile(r"(?is)\burl\s*\(\s*([^)]*)\)")
_CSS_IMPORT_RE: Final[re.Pattern[str]] = re.compile(r"(?is)@import\b")
_CSS_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(
    r"\\(?:(?P<hex>[0-9a-fA-F]{1,6})(?:\r\n|[ \t\r\n\f])?|"
    r"(?P<newline>\r\n|[\n\r\f])|(?P<char>.))",
    re.DOTALL,
)
_HTML_LOOKS_LIKE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?is)^\s*(?:<!doctype\s+html\b|<html\b|<head\b|<body\b|<script\b)"
)
_CSV_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"(?i)\.csv$")
_KNOWN_EXTENSION_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)\.(?:html|csv|css|png|jpe?g|gif|webp|woff2?)(?:\.|$)"
)
_INVALID_PERCENT_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"%(?![0-9a-fA-F]{2})")

_IMAGE_KINDS: Final[frozenset[UploadKind]] = frozenset(
    {UploadKind.PNG, UploadKind.JPEG, UploadKind.GIF, UploadKind.WEBP}
)
_FONT_KINDS: Final[frozenset[UploadKind]] = frozenset({UploadKind.WOFF, UploadKind.WOFF2})
_SUPPORTING_KINDS: Final[frozenset[UploadKind]] = frozenset(
    {UploadKind.CSV, UploadKind.CSS, UploadKind.IMAGE, UploadKind.FONT}
)
_TEXT_KINDS: Final[frozenset[UploadKind]] = frozenset(
    {UploadKind.HTML, UploadKind.CSV, UploadKind.CSS}
)
_BINARY_SIGNATURES: Final[Mapping[str, tuple[tuple[int, bytes], ...]]] = MappingProxyType(
    {
        "image/png": ((0, b"\x89PNG\r\n\x1a\n"),),
        "image/jpeg": ((0, b"\xff\xd8"),),
        "image/gif": ((0, b"GIF87a"),),
        "image/webp": ((0, b"RIFF"), (8, b"WEBP")),
        "font/woff": ((0, b"wOFF"),),
        "font/woff2": ((0, b"wOF2"),),
    }
)


def stage_upload(
    parts: Iterable[UploadPart], *, limits: UploadLimits | None = None
) -> StagedUpload:
    """Validate and stage one HTML part plus optional flat package assets.

    No database or durable artifact storage is touched until this function returns successfully.
    Temporary files are closed on every rejected stream, parser, or staging path.
    """

    policy = limits or UploadLimits()
    manifest = _build_manifest(parts, policy)
    artifact_kinds = MappingProxyType({entry.name.comparison_key: entry.kind for entry in manifest})
    staged: list[StagedUploadFile] = []
    total_size = 0
    referenced: set[str] = set()
    try:
        for entry in manifest:
            staged_file, total_size, file_references = _stage_entry(
                entry,
                artifact_kinds=artifact_kinds,
                total_size=total_size,
                limits=policy,
            )
            staged.append(staged_file)
            referenced.update(file_references)
    except BaseException:
        _close_staged_safely(staged)
        raise
    return StagedUpload(tuple(staged), total_size, frozenset(referenced))


def prepare_upload(
    parts: Iterable[UploadPart], *, limits: UploadLimits | None = None
) -> StagedUpload:
    """Readable alias for :func:`stage_upload` used by service callers."""

    return stage_upload(parts, limits=limits)


def _build_manifest(
    parts: Iterable[UploadPart], limits: UploadLimits
) -> tuple[_ManifestEntry, ...]:
    try:
        iterator = iter(parts)
    except Exception as error:
        raise UploadValidationError(UploadIssue(UploadIssueCode.MALFORMED_MULTIPART)) from error

    manifest: list[_ManifestEntry] = []
    seen_names: set[str] = set()
    html_count = 0
    while True:
        index = len(manifest)
        try:
            raw_part = next(iterator)
        except StopIteration:
            break
        except Exception as error:
            raise UploadValidationError(
                UploadIssue(UploadIssueCode.MALFORMED_MULTIPART, index)
            ) from error
        if index >= limits.max_files:
            raise UploadValidationError(UploadIssue(UploadIssueCode.TOO_MANY_FILES, index))
        part = _coerce_part(raw_part, index)
        name = _normalize_part_filename(part.filename, index)
        kind = _classify_filename(name.display, index)
        media_type = _media_type_for_filename(name.display, index)
        _validate_declared_media_type(part.media_type, kind, media_type, index)
        if name.comparison_key in seen_names:
            raise UploadValidationError(UploadIssue(UploadIssueCode.DUPLICATE_FILENAME, index))
        seen_names.add(name.comparison_key)
        if kind == UploadKind.HTML:
            html_count += 1
        manifest.append(_ManifestEntry(index, part, kind, name, media_type))

    if html_count == 0:
        raise UploadValidationError(UploadIssue(UploadIssueCode.MISSING_HTML))
    if html_count > 1:
        raise UploadValidationError(UploadIssue(UploadIssueCode.MULTIPLE_HTML))
    return tuple(manifest)


def _coerce_part(raw_part: object, index: int) -> UploadPart:
    if isinstance(raw_part, UploadPart):
        return raw_part
    raise UploadValidationError(UploadIssue(UploadIssueCode.MALFORMED_MULTIPART, index))


def _normalize_part_filename(raw_filename: object, index: int) -> LogicalName:
    if isinstance(raw_filename, bytes):
        try:
            filename = raw_filename.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise UploadValidationError(
                UploadIssue(UploadIssueCode.INVALID_FILENAME, index, UploadIssueField.FILENAME)
            ) from error
    elif isinstance(raw_filename, str):
        filename = raw_filename
    else:
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.INVALID_FILENAME, index, UploadIssueField.FILENAME)
        )
    try:
        filename.encode("utf-8", errors="strict")
        return normalize_logical_name(filename)
    except (InvalidLogicalName, UnicodeError) as error:
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.INVALID_FILENAME, index, UploadIssueField.FILENAME)
        ) from error


def _classify_filename(filename: str, index: int) -> UploadKind:
    dot = filename.rfind(".")
    if dot <= 0:
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.EXTENSION_MISMATCH, index, UploadIssueField.FILENAME)
        )
    suffix = filename[dot + 1 :].casefold()
    stem = filename[:dot].casefold()
    if _KNOWN_EXTENSION_SEGMENT_RE.search(stem):
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.EXTENSION_MISMATCH, index, UploadIssueField.FILENAME)
        )
    kind = UPLOAD_EXTENSION_TO_KIND.get(suffix)
    if kind is None:
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.EXTENSION_MISMATCH, index, UploadIssueField.FILENAME)
        )
    return kind


def _media_type_for_filename(filename: str, index: int) -> str:
    dot = filename.rfind(".")
    suffix = filename[dot + 1 :].casefold() if dot >= 0 else ""
    media_type = UPLOAD_EXTENSION_TO_MEDIA_TYPE.get(suffix)
    if media_type is None:
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.EXTENSION_MISMATCH, index, UploadIssueField.FILENAME)
        )
    return media_type


_DECLARED_MEDIA_TYPE_ALIASES: Final[Mapping[UploadKind, frozenset[str]]] = MappingProxyType(
    {
        UploadKind.CSV: frozenset({"application/vnd.ms-excel"}),
        UploadKind.IMAGE: frozenset({"application/octet-stream"}),
        UploadKind.FONT: frozenset({"application/octet-stream", "application/font-woff"}),
    }
)


def _validate_declared_media_type(
    raw_media_type: object, kind: UploadKind, expected: str, index: int
) -> None:
    if raw_media_type is None:
        return
    if not isinstance(raw_media_type, str) or not raw_media_type.strip():
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.MEDIA_TYPE_MISMATCH, index, UploadIssueField.MEDIA_TYPE)
        )
    pieces = [piece.strip() for piece in raw_media_type.split(";")]
    media_type = pieces[0].casefold()
    accepted = {expected, *_DECLARED_MEDIA_TYPE_ALIASES.get(kind, frozenset())}
    if media_type not in accepted:
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.MEDIA_TYPE_MISMATCH, index, UploadIssueField.MEDIA_TYPE)
        )
    if kind not in _TEXT_KINDS and len(pieces) > 1:
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.MEDIA_TYPE_MISMATCH, index, UploadIssueField.MEDIA_TYPE)
        )
    for parameter in pieces[1:]:
        if not parameter:
            raise UploadValidationError(
                UploadIssue(UploadIssueCode.MEDIA_TYPE_MISMATCH, index, UploadIssueField.MEDIA_TYPE)
            )
        key, separator, value = parameter.partition("=")
        if (
            not separator
            or key.casefold() != "charset"
            or value.strip().strip('"').casefold() != "utf-8"
        ):
            raise UploadValidationError(
                UploadIssue(UploadIssueCode.MEDIA_TYPE_MISMATCH, index, UploadIssueField.MEDIA_TYPE)
            )


def _stage_entry(
    entry: _ManifestEntry,
    *,
    artifact_kinds: Mapping[str, UploadKind],
    total_size: int,
    limits: UploadLimits,
) -> tuple[StagedUploadFile, int, frozenset[str]]:
    try:
        temporary = cast(BinaryIO, tempfile.TemporaryFile(mode="w+b"))
    except Exception as error:
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.STAGING_ERROR, entry.index)
        ) from error

    byte_size = 0
    digest = hashlib.sha256()
    decoder = (
        codecs.getincrementaldecoder("utf-8-sig")("strict") if entry.kind in _TEXT_KINDS else None
    )
    html_validator = (
        _HtmlValidator(artifact_kinds, limits, entry.index)
        if entry.kind == UploadKind.HTML
        else None
    )
    css_validator = (
        _CssValidator(artifact_kinds, limits, entry.index) if entry.kind == UploadKind.CSS else None
    )
    chunk_count = 0
    try:
        source = _source_iterator(entry.part.chunks)
        for raw_chunk in source:
            chunk_count += 1
            if chunk_count > limits.max_chunks_per_file:
                _raise_issue(UploadIssueCode.TOO_MANY_CHUNKS, entry.index)
            if not isinstance(raw_chunk, bytes):
                _raise_issue(UploadIssueCode.NON_BYTES_CHUNK, entry.index)
            chunk = raw_chunk
            next_size = byte_size + len(chunk)
            if next_size > limits.max_file_bytes:
                _raise_issue(UploadIssueCode.FILE_TOO_LARGE, entry.index)
            next_total = total_size + len(chunk)
            if next_total > limits.max_total_bytes:
                _raise_issue(UploadIssueCode.TOTAL_TOO_LARGE, entry.index)
            if decoder is not None:
                decoded = _decode_chunk(decoder, chunk, entry.index)
                _validate_decoded_text(decoded, entry.index)
                if html_validator is not None:
                    html_validator.feed(decoded)
                if css_validator is not None:
                    css_validator.feed(decoded)
            _write_all(temporary, chunk, entry.index)
            digest.update(chunk)
            byte_size = next_size
            total_size = next_total

        if decoder is not None:
            decoded = _decode_chunk(decoder, b"", entry.index, final=True)
            _validate_decoded_text(decoded, entry.index)
            if html_validator is not None:
                html_validator.feed(decoded)
            if css_validator is not None:
                css_validator.feed(decoded)
        if byte_size == 0:
            _raise_issue(UploadIssueCode.EMPTY_FILE, entry.index)
        references: frozenset[str]
        if html_validator is not None:
            references = html_validator.finish()
        elif css_validator is not None:
            references = css_validator.finish()
        else:
            if entry.kind == UploadKind.CSV:
                _validate_csv(temporary, entry.index, limits)
            else:
                _validate_binary_signature(temporary, entry.media_type, entry.index)
            references = frozenset()
        temporary.flush()
        temporary.seek(0)
        staged = StagedUploadFile(
            kind=entry.kind,
            logical_name=entry.name,
            byte_size=byte_size,
            sha256=digest.hexdigest(),
            _stream=temporary,
            media_type=entry.media_type,
        )
        return staged, total_size, references
    except UploadRejected:
        _close_one_safely(temporary)
        raise
    except Exception as error:
        _close_one_safely(temporary)
        raise UploadValidationError(
            UploadIssue(UploadIssueCode.STREAM_ERROR, entry.index)
        ) from error
    except BaseException:
        _close_one_safely(temporary)
        raise


def _source_iterator(
    chunks: Iterable[bytes] | Callable[[], Iterable[bytes]],
) -> Iterator[object]:
    try:
        source = chunks() if callable(chunks) else chunks
        return iter(source)
    except Exception as error:
        raise OSError("could not open upload byte stream") from error


def _decode_chunk(
    decoder: codecs.IncrementalDecoder, chunk: bytes, index: int, *, final: bool = False
) -> str:
    try:
        return decoder.decode(chunk, final=final)
    except UnicodeDecodeError as error:
        raise UploadValidationError(UploadIssue(UploadIssueCode.INVALID_UTF8, index)) from error


def _validate_decoded_text(text: str, index: int) -> None:
    if "\x00" in text:
        _raise_issue(UploadIssueCode.BINARY_CONTENT, index)
    for character in text:
        codepoint = ord(character)
        if (codepoint < 0x20 and character not in "\t\n\r") or codepoint == 0x7F:
            _raise_issue(UploadIssueCode.BINARY_CONTENT, index)


def _validate_binary_signature(file: BinaryIO, media_type: str, index: int) -> None:
    """Check a bounded magic prefix without decoding an untrusted binary as text."""

    checks = _BINARY_SIGNATURES[media_type]
    try:
        file.seek(0)
        required = max(offset + len(signature) for offset, signature in checks)
        prefix = file.read(required)
        if not isinstance(prefix, bytes) or len(prefix) < required:
            _raise_issue(UploadIssueCode.MEDIA_TYPE_MISMATCH, index)
        if media_type == "image/gif":
            if prefix[:6] not in {b"GIF87a", b"GIF89a"}:
                _raise_issue(UploadIssueCode.MEDIA_TYPE_MISMATCH, index)
        elif any(
            prefix[offset : offset + len(signature)] != signature for offset, signature in checks
        ):
            _raise_issue(UploadIssueCode.MEDIA_TYPE_MISMATCH, index)
        file.seek(0)
    except UploadRejected:
        raise
    except Exception as error:
        raise UploadValidationError(UploadIssue(UploadIssueCode.STAGING_ERROR, index)) from error


def _write_all(file: BinaryIO, chunk: bytes, index: int) -> None:
    remaining = memoryview(chunk)
    try:
        while remaining:
            written = file.write(remaining)
            if written is None or written <= 0:
                raise OSError("staging write made no progress")
            remaining = remaining[written:]
    except UploadRejected:
        raise
    except Exception as error:
        raise UploadValidationError(UploadIssue(UploadIssueCode.STAGING_ERROR, index)) from error


def _validate_csv(file: BinaryIO, index: int, limits: UploadLimits) -> None:
    try:
        file.seek(0)
        text_stream = io.TextIOWrapper(file, encoding="utf-8-sig", errors="strict", newline="")
        lines = _CountingTextLines(text_stream)
        row_count = 0
        previous_record_end = 0
        expected_fields: int | None = None
        saw_value = False
        prefix = ""
        try:
            with _CSV_FIELD_LIMIT_LOCK:
                previous_field_limit = csv.field_size_limit()
                # The file cap is the hard allocation bound.  Keep the stdlib parser just
                # above the byte-level policy so an oversized UTF-8 field receives the stable
                # field-specific issue rather than an implementation-specific csv.Error.
                csv.field_size_limit(max(1, limits.max_file_bytes, limits.max_csv_field_bytes))
                try:
                    reader = csv.reader(lines, strict=True)
                    for row in reader:
                        row_count += 1
                        if row_count > limits.max_csv_rows:
                            _raise_issue(UploadIssueCode.CSV_TOO_MANY_ROWS, index)
                        record_bytes = lines.byte_count - previous_record_end
                        previous_record_end = lines.byte_count
                        if record_bytes > limits.max_csv_record_bytes:
                            _raise_issue(UploadIssueCode.CSV_RECORD_TOO_LARGE, index)
                        if len(row) > limits.max_csv_fields:
                            _raise_issue(UploadIssueCode.CSV_TOO_MANY_FIELDS, index)
                        if row:
                            if expected_fields is None:
                                expected_fields = len(row)
                            elif len(row) != expected_fields:
                                _raise_issue(UploadIssueCode.CSV_MALFORMED, index)
                        for field in row:
                            encoded_size = len(field.encode("utf-8"))
                            if encoded_size > limits.max_csv_field_bytes:
                                _raise_issue(UploadIssueCode.CSV_FIELD_TOO_LARGE, index)
                            if field:
                                saw_value = True
                        if len(prefix) < 512:
                            prefix += "\n".join(row)[: 512 - len(prefix)]
                finally:
                    csv.field_size_limit(previous_field_limit)
        finally:
            try:
                text_stream.detach()
            except ValueError, AttributeError:
                pass
        if not saw_value:
            _raise_issue(UploadIssueCode.EMPTY_FILE, index)
        if _HTML_LOOKS_LIKE_RE.search(prefix):
            _raise_issue(UploadIssueCode.MEDIA_TYPE_MISMATCH, index)
    except UploadRejected:
        raise
    except UnicodeDecodeError as error:
        raise UploadValidationError(UploadIssue(UploadIssueCode.INVALID_UTF8, index)) from error
    except csv.Error as error:
        raise UploadValidationError(UploadIssue(UploadIssueCode.CSV_MALFORMED, index)) from error
    except Exception as error:
        raise UploadValidationError(UploadIssue(UploadIssueCode.CSV_MALFORMED, index)) from error


class _CountingTextLines:
    def __init__(self, stream: io.TextIOBase) -> None:
        self._stream = stream
        self.byte_count = 0

    def __iter__(self) -> _CountingTextLines:
        return self

    def __next__(self) -> str:
        line = next(self._stream)
        self.byte_count += len(line.encode("utf-8"))
        return line


class _CssValidator:
    """Bounded CSS scanner that only permits package-local image/font URLs."""

    def __init__(
        self, artifact_kinds: Mapping[str, UploadKind], limits: UploadLimits, index: int
    ) -> None:
        self._artifact_kinds = artifact_kinds
        self._limits = limits
        self._index = index
        self._references: set[str] = set()
        self._tail = ""

    def feed(self, data: str) -> None:
        if not data:
            return
        combined = self._tail + data
        self._inspect(combined)
        self._tail = combined[-4096:]

    def finish(self) -> frozenset[str]:
        # The final tail catches a URL whose opener appeared at a chunk boundary.  Repeated
        # findings are harmless because references are collected in a set.
        self._inspect(self._tail)
        return frozenset(self._references)

    def _inspect(self, combined: str) -> None:
        decoded = _decode_css_escapes(combined)
        if _CSS_IMPORT_RE.search(decoded):
            _raise_issue(
                UploadIssueCode.EXTERNAL_DEPENDENCY,
                self._index,
                field=UploadIssueField.CSS_IMPORT,
            )
        if _EXTERNAL_URL_TOKEN_RE.search(decoded):
            _raise_issue(
                UploadIssueCode.EXTERNAL_DEPENDENCY,
                self._index,
                field=UploadIssueField.CSS_EXTERNAL_URL,
            )
        for match in _CSS_URL_RE.finditer(decoded):
            raw_url = match.group(1).strip()
            if len(raw_url) >= 2 and raw_url[0] == raw_url[-1] and raw_url[0] in "\"'":
                raw_url = raw_url[1:-1]
            elif any(character in raw_url for character in "\"'"):
                _raise_issue(UploadIssueCode.INVALID_ASSET_REFERENCE, self._index)
            if not raw_url or any(character.isspace() for character in raw_url):
                _raise_issue(UploadIssueCode.INVALID_ASSET_REFERENCE, self._index)
            if _SAFE_DATA_URL_RE.match(raw_url) or raw_url.casefold().startswith("blob:"):
                continue
            reference = _resolve_asset_reference(
                raw_url,
                self._artifact_kinds,
                self._index,
                allowed_kinds=_IMAGE_KINDS | _FONT_KINDS,
                external_field=UploadIssueField.CSS_EXTERNAL_URL,
            )
            self._references.add(reference)


def _decode_css_escapes(value: str) -> str:
    """Decode CSS escapes before applying the package URL policy.

    CSS permits escapes in identifiers and URL tokens (for example ``u\\72l``). Applying the
    policy to decoded text prevents escaped ``url``/``@import``/scheme spellings from bypassing
    the same checks used for their ordinary forms while still permitting escaped local names.
    """

    def replace(match: re.Match[str]) -> str:
        hexadecimal = match.group("hex")
        if hexadecimal is not None:
            codepoint = int(hexadecimal, 16)
            if codepoint == 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                return "\N{REPLACEMENT CHARACTER}"
            return chr(codepoint)
        if match.group("newline") is not None:
            return ""
        return match.group("char") or ""

    return _CSS_ESCAPE_RE.sub(replace, value)


class _HtmlValidator(HTMLParser):
    """Strict-enough document/URL policy parser with explicit work and nesting bounds."""

    def __init__(
        self, artifact_kinds: Mapping[str, UploadKind], limits: UploadLimits, index: int
    ) -> None:
        super().__init__(convert_charrefs=False)
        self._artifact_kinds = artifact_kinds
        self._limits = limits
        self._index = index
        self._stack: list[str] = []
        self._root_seen = False
        self._root_closed = False
        self._doctype_seen = False
        self._head_seen = False
        self._body_seen = False
        self._token_count = 0
        self._references: set[str] = set()
        self._comment_open = False
        self._markup_tail = ""
        self._script_tail = ""
        self._css_validator = _CssValidator(artifact_kinds, limits, index)

    def feed(self, data: str) -> None:
        self._track_unterminated_comment(data)
        try:
            super().feed(data)
        except UploadRejected:
            raise
        except Exception as error:
            raise UploadValidationError(
                UploadIssue(UploadIssueCode.HTML_MALFORMED, self._index)
            ) from error

    def close(self) -> None:
        try:
            super().close()
        except UploadRejected:
            raise
        except Exception as error:
            raise UploadValidationError(
                UploadIssue(UploadIssueCode.HTML_MALFORMED, self._index)
            ) from error
        if self.rawdata or self._comment_open:
            raise UploadValidationError(UploadIssue(UploadIssueCode.HTML_MALFORMED, self._index))

    def finish(self) -> frozenset[str]:
        self.close()
        if not self._root_seen or not self._root_closed or self._stack:
            _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
        self._references.update(self._css_validator.finish())
        return frozenset(self._references)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_tag(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        self._token()
        lowered = tag.casefold()
        if lowered in _HTML_VOID_ELEMENTS or not self._stack or self._stack[-1] != lowered:
            _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
        self._stack.pop()
        if lowered == "html":
            self._root_closed = True

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if not self._stack:
            if data.strip():
                _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
            return
        if self._stack[-1] in {"script", "style"}:
            self._inspect_code(data, css=self._stack[-1] == "style")
            return
        if "<" in data:
            _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)

    def handle_comment(self, data: str) -> None:
        del data

    def handle_decl(self, decl: str) -> None:
        self._token()
        if self._stack or self._doctype_seen or decl.casefold() != "doctype html":
            _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
        self._doctype_seen = True

    def handle_pi(self, data: str) -> None:
        del data
        _raise_issue(
            UploadIssueCode.EXTERNAL_DEPENDENCY,
            self._index,
            field=UploadIssueField.HTML_PROCESSING_INSTRUCTION,
        )

    def unknown_decl(self, data: str) -> None:
        del data
        _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)

    def _start_tag(
        self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool
    ) -> None:
        self._token()
        lowered = tag.casefold()
        if lowered in _HTML_FORBIDDEN_ELEMENTS:
            _raise_issue(
                UploadIssueCode.EXTERNAL_DEPENDENCY,
                self._index,
                field=UploadIssueField.HTML_EMBEDDED_CONTENT,
            )
        if lowered == "form":
            _raise_issue(
                UploadIssueCode.EXTERNAL_DEPENDENCY,
                self._index,
                field=UploadIssueField.HTML_FORM_ACTION,
            )
        if not self._stack:
            if lowered != "html" or self._root_seen or self._root_closed:
                _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
            self._root_seen = True
        elif lowered == "html":
            _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
        if lowered == "head":
            if self._stack != ["html"] or self._head_seen or self._body_seen:
                _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
            self._head_seen = True
        if lowered == "body":
            if self._stack != ["html"] or self._body_seen:
                _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
            self._body_seen = True
        self._validate_attributes(lowered, attrs)
        if lowered in _HTML_VOID_ELEMENTS:
            return
        if self_closing:
            _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
        if len(self._stack) >= self._limits.max_html_nesting:
            _raise_issue(UploadIssueCode.HTML_TOO_COMPLEX, self._index)
        self._stack.append(lowered)

    def _validate_attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names: set[str] = set()
        values: dict[str, str] = {}
        for raw_name, raw_value in attrs:
            name = raw_name.casefold()
            if name in names:
                _raise_issue(UploadIssueCode.HTML_MALFORMED, self._index)
            names.add(name)
            values[name] = "" if raw_value is None else raw_value

        link_kinds: frozenset[UploadKind] | None = None
        if tag == "link":
            rel = values.get("rel", "")
            rel_tokens = {token.casefold() for token in rel.split()}
            if "stylesheet" in rel_tokens:
                link_kinds = frozenset({UploadKind.CSS})
            elif rel_tokens & {"icon", "apple-touch-icon"}:
                link_kinds = _IMAGE_KINDS
            if link_kinds is None or "href" not in values:
                _raise_issue(
                    UploadIssueCode.EXTERNAL_DEPENDENCY,
                    self._index,
                    field=UploadIssueField.HTML_LINK,
                )

        for name, value in values.items():
            if tag == "script" and name == "src":
                _raise_issue(
                    UploadIssueCode.EXTERNAL_DEPENDENCY,
                    self._index,
                    field=UploadIssueField.HTML_SCRIPT_SOURCE,
                )
            if name == "manifest":
                _raise_issue(
                    UploadIssueCode.EXTERNAL_DEPENDENCY,
                    self._index,
                    field=UploadIssueField.HTML_MANIFEST,
                )
            if name in _FORM_URL_ATTRIBUTES:
                _raise_issue(
                    UploadIssueCode.EXTERNAL_DEPENDENCY,
                    self._index,
                    field=UploadIssueField.HTML_FORM_ACTION,
                )
            if name in {"srcset", "imagesrcset"}:
                _raise_issue(
                    UploadIssueCode.EXTERNAL_DEPENDENCY,
                    self._index,
                    field=UploadIssueField.HTML_RESPONSIVE_SOURCE,
                )
            if name in _URL_ATTRIBUTES:
                if tag == "link" and name == "href":
                    assert link_kinds is not None
                    allowed = link_kinds
                else:
                    allowed = self._allowed_attribute_kinds(tag, name)
                self._inspect_url_attribute(value, tag, name, allowed)
            elif name == "style":
                self._inspect_code(value, css=True)
            elif name.startswith("on"):
                self._inspect_code(value, css=False)
            elif tag == "meta" and name == "content":
                if _EXTERNAL_URL_TOKEN_RE.search(value):
                    _raise_issue(
                        UploadIssueCode.EXTERNAL_DEPENDENCY,
                        self._index,
                        field=UploadIssueField.HTML_META_URL,
                    )
            elif tag == "meta" and name == "http-equiv" and value.casefold() == "refresh":
                _raise_issue(
                    UploadIssueCode.EXTERNAL_DEPENDENCY,
                    self._index,
                    field=UploadIssueField.HTML_META_REFRESH,
                )

    def _allowed_attribute_kinds(self, tag: str, attribute: str) -> frozenset[UploadKind]:
        if attribute == "href" and tag in {"a", "area"}:
            return _SUPPORTING_KINDS
        if attribute in {"background", "poster"}:
            return _IMAGE_KINDS
        if attribute == "src" and tag in {"img", "source"}:
            return _IMAGE_KINDS
        return frozenset()

    def _inspect_url_attribute(
        self,
        value: str,
        tag: str,
        attribute: str,
        allowed_kinds: frozenset[UploadKind],
    ) -> None:
        compatibility_csv = allowed_kinds == frozenset({UploadKind.CSV})
        invalid_code = (
            UploadIssueCode.INVALID_CSV_REFERENCE
            if compatibility_csv
            else UploadIssueCode.INVALID_ASSET_REFERENCE
        )
        if not value or any(character.isspace() for character in value):
            _raise_issue(invalid_code, self._index)
        if attribute in _FORM_URL_ATTRIBUTES:
            _raise_issue(
                UploadIssueCode.EXTERNAL_DEPENDENCY,
                self._index,
                field=UploadIssueField.HTML_FORM_ACTION,
            )
        if value.startswith("#"):
            if attribute == "href" and tag in {"a", "area"}:
                return
            _raise_issue(invalid_code, self._index)
        if allowed_kinds and (
            _safe_data_url_for_kinds(value, allowed_kinds) or value.casefold().startswith("blob:")
        ):
            return
        reference = _resolve_asset_reference(
            value,
            self._artifact_kinds,
            self._index,
            allowed_kinds=allowed_kinds,
            csv_compatibility=compatibility_csv,
            external_field=UploadIssueField.EXTERNAL_REFERENCE,
        )
        self._references.add(reference)

    def _inspect_code(self, data: str, *, css: bool) -> None:
        if css:
            self._css_validator.feed(data)
            return
        combined = self._script_tail + data
        self._script_tail = combined[-4096:]
        if _EXTERNAL_URL_TOKEN_RE.search(combined):
            _raise_issue(
                UploadIssueCode.EXTERNAL_DEPENDENCY,
                self._index,
                field=UploadIssueField.INLINE_SCRIPT_URL,
            )
        if _DYNAMIC_NETWORK_RE.search(combined):
            _raise_issue(
                UploadIssueCode.EXTERNAL_DEPENDENCY,
                self._index,
                field=UploadIssueField.INLINE_NETWORK_API,
            )
        matches = [
            *_CSV_CALL_RE.finditer(combined),
            *_CSV_TEMPLATE_CALL_RE.finditer(combined),
        ]
        matches.sort(key=lambda match: match.start())
        for match in matches:
            after_literal = combined[match.end() :]
            if re.match(r"\s*(?:\)|,)", after_literal) is None:
                _raise_issue(
                    UploadIssueCode.EXTERNAL_DEPENDENCY,
                    self._index,
                    field=UploadIssueField.DYNAMIC_DATA_REFERENCE,
                )
            if "${" in match.group("url"):
                # The content CSP limits fetch to the isolated content origin, and artifact
                # delivery independently authorizes only this Revision's package. A template
                # expression therefore cannot widen access, but its final filename is not
                # statically knowable and is intentionally omitted from the reference summary.
                continue
            reference = _resolve_csv_reference(
                match.group("url"),
                self._artifact_kinds,
                self._index,
                external_field=UploadIssueField.DYNAMIC_DATA_REFERENCE,
            )
            self._references.add(reference)
        if _FETCH_RE.search(combined):
            covered = {match.start() for match in matches}
            for match in _FETCH_RE.finditer(combined):
                if not any(start <= match.start() < start + 4096 for start in covered):
                    _raise_issue(
                        UploadIssueCode.EXTERNAL_DEPENDENCY,
                        self._index,
                        field=_dynamic_data_issue_field(combined, match.end()),
                    )
        if _D3_CSV_RE.search(combined):
            covered = {match.start() for match in matches}
            for match in _D3_CSV_RE.finditer(combined):
                if not any(start <= match.start() < start + 4096 for start in covered):
                    _raise_issue(
                        UploadIssueCode.EXTERNAL_DEPENDENCY,
                        self._index,
                        field=_dynamic_data_issue_field(combined, match.end()),
                    )

    def _track_unterminated_comment(self, data: str) -> None:
        combined = self._markup_tail + data
        cursor = 0
        while True:
            if self._comment_open:
                end = combined.find("-->", cursor)
                if end < 0:
                    self._markup_tail = combined[-4:]
                    return
                self._comment_open = False
                cursor = end + 3
                continue
            start = combined.find("<!--", cursor)
            if start < 0:
                self._markup_tail = combined[-4:]
                return
            self._comment_open = True
            cursor = start + 4

    def _token(self) -> None:
        self._token_count += 1
        if self._token_count > self._limits.max_html_tokens:
            _raise_issue(UploadIssueCode.HTML_TOO_COMPLEX, self._index)


def _safe_data_url_for_kinds(value: str, allowed_kinds: frozenset[UploadKind]) -> bool:
    if _SAFE_DATA_URL_RE.match(value) is None:
        return False
    if value.casefold().startswith("data:font/"):
        return bool(allowed_kinds & _FONT_KINDS)
    return bool(allowed_kinds & _IMAGE_KINDS)


def _dynamic_data_issue_field(source: str, argument_start: int) -> UploadIssueField:
    """Classify an unsupported fetch/d3.csv argument without retaining uploaded content."""

    argument = source[argument_start:].lstrip()
    if argument.startswith("`"):
        return UploadIssueField.DATA_TEMPLATE_LITERAL
    if re.match(r"[A-Za-z_$][A-Za-z0-9_$]*\b", argument):
        return UploadIssueField.DATA_IDENTIFIER
    return UploadIssueField.DATA_EXPRESSION


def _resolve_csv_reference(
    raw_value: str,
    artifact_kinds: Mapping[str, UploadKind],
    index: int,
    *,
    external_field: UploadIssueField = UploadIssueField.DYNAMIC_DATA_REFERENCE,
) -> str:
    return _resolve_asset_reference(
        raw_value,
        artifact_kinds,
        index,
        allowed_kinds=frozenset({UploadKind.CSV}),
        csv_compatibility=True,
        external_field=external_field,
    )


def _resolve_asset_reference(
    raw_value: str,
    artifact_kinds: Mapping[str, UploadKind],
    index: int,
    *,
    allowed_kinds: frozenset[UploadKind],
    csv_compatibility: bool = False,
    external_field: UploadIssueField = UploadIssueField.EXTERNAL_REFERENCE,
) -> str:
    invalid_code = (
        UploadIssueCode.INVALID_CSV_REFERENCE
        if csv_compatibility
        else UploadIssueCode.INVALID_ASSET_REFERENCE
    )
    missing_code = (
        UploadIssueCode.MISSING_CSV_REFERENCE
        if csv_compatibility
        else UploadIssueCode.MISSING_ASSET_REFERENCE
    )
    if raw_value.startswith("//") and len(raw_value) > 2 and raw_value[2].isalnum():
        _raise_issue(UploadIssueCode.EXTERNAL_DEPENDENCY, index, field=external_field)
    if not raw_value or raw_value.startswith(("/", "\\", "?")):
        _raise_issue(invalid_code, index)
    if "\\" in raw_value or _INVALID_PERCENT_ESCAPE_RE.search(raw_value):
        _raise_issue(invalid_code, index)
    try:
        parsed = urlsplit(raw_value)
    except ValueError as error:
        raise UploadValidationError(UploadIssue(invalid_code, index)) from error
    if parsed.scheme or parsed.netloc:
        _raise_issue(UploadIssueCode.EXTERNAL_DEPENDENCY, index, field=external_field)
    if parsed.query or parsed.fragment or "?" in raw_value or "#" in raw_value:
        _raise_issue(invalid_code, index)
    path = parsed.path
    if path.startswith("./"):
        path = path[2:]
    try:
        decoded_path = unquote_to_bytes(path).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as error:
        raise UploadValidationError(UploadIssue(invalid_code, index)) from error
    # Decode exactly once. Encoded separators/dot traversal and double-encoded values never
    # become logical names, while ordinary URL encoding such as ``sales%20data.csv`` works.
    if (
        not decoded_path
        or "/" in decoded_path
        or "\\" in decoded_path
        or "%" in decoded_path
        or decoded_path in {".", ".."}
    ):
        _raise_issue(invalid_code, index)
    if csv_compatibility and not _CSV_SUFFIX_RE.search(decoded_path):
        _raise_issue(invalid_code, index)
    try:
        normalized = normalize_logical_name(decoded_path)
    except InvalidLogicalName as error:
        raise UploadValidationError(UploadIssue(invalid_code, index)) from error
    kind = artifact_kinds.get(normalized.comparison_key)
    if kind is None:
        _raise_issue(missing_code, index)
    if kind not in allowed_kinds:
        _raise_issue(invalid_code, index)
    return normalized.comparison_key


def _raise_issue(
    code: UploadIssueCode,
    index: int | None = None,
    *,
    field: UploadIssueField | None = None,
) -> NoReturn:
    raise UploadValidationError(UploadIssue(code, index, field))


def _close_one_safely(file: BinaryIO) -> None:
    try:
        file.close()
    except BaseException:
        pass


def _close_staged_safely(files: Iterable[StagedUploadFile]) -> None:
    for file in files:
        _close_one_safely(file._stream)


__all__ = [
    "EXTENSION_TO_KIND",
    "EXTENSION_TO_MEDIA_TYPE",
    "KIND_TO_MEDIA_TYPE",
    "KIND_TO_MEDIA_TYPES",
    "MEDIA_TYPE_BY_EXTENSION",
    "UPLOAD_EXTENSION_MAP",
    "UPLOAD_EXTENSION_TO_KIND",
    "UPLOAD_EXTENSION_TO_MEDIA_TYPE",
    "UPLOAD_KIND_TO_MEDIA_TYPE",
    "UPLOAD_KIND_TO_MEDIA_TYPES",
    "StagedUpload",
    "StagedUploadFile",
    "UploadIssue",
    "UploadIssueCode",
    "UploadKind",
    "UploadLimits",
    "UploadPart",
    "UploadRejected",
    "UploadValidationError",
    "ValidatedUpload",
    "prepare_upload",
    "stage_upload",
]
