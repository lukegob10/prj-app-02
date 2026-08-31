from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from django.db import IntegrityError

import agora.persistence.services as persistence_services
from agora.persistence.models import (
    Artifact,
    AuditEvent,
    Dashboard,
    Revision,
    StorageReservation,
    User,
)
from agora.persistence.names import normalize_logical_name
from agora.persistence.storage import (
    ArtifactStorageError,
    FilesystemArtifactStorage,
    StorageCleanupRequired,
    StorageKey,
    StorageWriteCommitted,
    StoredArtifact,
)
from agora.persistence.uploads import create_upload_revision
from agora.uploads import (
    UPLOAD_EXTENSION_MAP,
    UPLOAD_EXTENSION_TO_KIND,
    UPLOAD_EXTENSION_TO_MEDIA_TYPE,
    StagedUpload,
    StagedUploadFile,
    UploadIssueCode,
    UploadIssueField,
    UploadKind,
    UploadLimits,
    UploadPart,
    UploadRejected,
    stage_upload,
)


def part(
    filename: str | bytes,
    content: bytes | Iterable[bytes] | Callable[[], Iterable[bytes]],
    *,
    media_type: str | None = None,
    content_length: int | None = None,
) -> UploadPart:
    chunks = [content] if isinstance(content, bytes) else content
    return UploadPart(filename, chunks, media_type, content_length)


def valid_html(body: bytes = b"") -> bytes:
    return b"<html><body>" + body + b"</body></html>"


def valid_csv(value: bytes = b"1") -> bytes:
    return b"name,value\nitem," + value + b"\n"


def valid_binary(extension: str) -> bytes:
    signatures = {
        "png": b"\x89PNG\r\n\x1a\n",
        "jpg": b"\xff\xd8",
        "jpeg": b"\xff\xd8",
        "gif": b"GIF89a",
        "webp": b"RIFF\x00\x00\x00\x00WEBP",
        "woff": b"wOFF",
        "woff2": b"wOF2",
    }
    return signatures[extension] + b"\x00\xffpackage-bytes"


def rejection(parts: Iterable[UploadPart], *, limits: UploadLimits | None = None) -> UploadRejected:
    with pytest.raises(UploadRejected) as raised:
        staged = stage_upload(parts, limits=limits)
        staged.close()
    return raised.value


def test_valid_html_without_csv_is_staged_unchanged() -> None:
    content = b"\xef\xbb\xbf<!doctype html><html><body>safe</body></html>"

    staged = stage_upload(
        [
            part(
                "Dashboard.HTML",
                [b"\xef\xbb", b"\xbf<!doctype html>", b"<html><body>safe</body></html>"],
                media_type="text/html; charset=utf-8",
                content_length=1,
            )
        ]
    )
    try:
        assert isinstance(staged, StagedUpload)
        assert staged.total_size == len(content)
        assert staged.referenced_csv_names == frozenset()
        item = staged.files[0]
        assert item.kind == UploadKind.HTML
        assert item.filename == "Dashboard.HTML"
        assert item.byte_size == len(content)
        assert item.sha256 == hashlib.sha256(content).hexdigest()
        assert b"".join(item.iter_chunks(3)) == content
    finally:
        staged.close()


def test_valid_csv_is_staged_and_references_are_normalized() -> None:
    html = valid_html(b'<script>fetch("./Positions.CSV")</script><a href="positions.csv">CSV</a>')
    csv_content = b'name,value\n"A\nB",=1+1\n'
    staged = stage_upload(
        [
            part("dashboard.html", [html[:7], b"", html[7:]], media_type="text/html"),
            part("Positions.csv", [csv_content[:2], csv_content[2:]], media_type="text/csv"),
        ]
    )
    try:
        assert staged.referenced_csv_names == frozenset({"positions.csv"})
        assert [item.kind for item in staged.files] == [UploadKind.HTML, UploadKind.CSV]
        assert b"".join(staged.files[1].iter_chunks()) == csv_content
    finally:
        staged.close()


def test_authoritative_extension_maps_cover_all_flat_package_types() -> None:
    assert set(UPLOAD_EXTENSION_TO_KIND) == {
        "html",
        "csv",
        "css",
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "woff",
        "woff2",
    }
    assert UPLOAD_EXTENSION_MAP["jpg"] == (UploadKind.IMAGE, "image/jpeg")
    assert UPLOAD_EXTENSION_MAP["woff2"] == (UploadKind.FONT, "font/woff2")
    assert UPLOAD_EXTENSION_TO_MEDIA_TYPE["css"] == "text/css"


@pytest.mark.parametrize(
    ("extension", "kind", "media_type"),
    [
        ("png", UploadKind.IMAGE, "image/png"),
        ("jpg", UploadKind.IMAGE, "image/jpeg"),
        ("jpeg", UploadKind.IMAGE, "image/jpeg"),
        ("gif", UploadKind.IMAGE, "image/gif"),
        ("webp", UploadKind.IMAGE, "image/webp"),
        ("woff", UploadKind.FONT, "font/woff"),
        ("woff2", UploadKind.FONT, "font/woff2"),
    ],
)
def test_binary_assets_are_byte_preserving_and_not_utf8_decoded(
    extension: str, kind: UploadKind, media_type: str
) -> None:
    content = valid_binary(extension)
    staged = stage_upload(
        [
            part("dashboard.html", [valid_html()]),
            part(
                f"asset.{extension}",
                [content[:2], b"", content[2:]],
                media_type="application/octet-stream",
            ),
        ]
    )
    try:
        item = staged.files[1]
        assert item.kind == kind
        assert item.media_type == media_type
        assert item.byte_size == len(content)
        assert item.sha256 == hashlib.sha256(content).hexdigest()
        assert b"".join(item.iter_chunks(2)) == content
    finally:
        staged.close()


@pytest.mark.parametrize(
    ("extension", "content"),
    [
        ("png", b"not a png"),
        ("jpg", b"not a jpeg"),
        ("gif", b"GIF"),
        ("webp", b"RIFF\x00\x00\x00\x00WEBX"),
        ("woff", b"not a font"),
        ("woff2", b"wOFF"),
    ],
)
def test_binary_signature_must_match_authoritative_extension(
    extension: str, content: bytes
) -> None:
    error = rejection(
        [part("dashboard.html", [valid_html()]), part(f"asset.{extension}", [content])]
    )
    assert error.issue.code == UploadIssueCode.MEDIA_TYPE_MISMATCH


def test_flat_package_links_and_general_references_are_typed() -> None:
    html = valid_html(
        b'<link rel="stylesheet" href="theme.css">'
        b'<link rel="icon" href="logo.PNG">'
        b'<img src="logo.PNG">'
        b'<script>fetch("./data.csv")</script>'
    )
    css = b'body{background:url("logo.PNG")}@font-face{src:url("screen.woff2")}'
    staged = stage_upload(
        [
            part("dashboard.html", [html]),
            part("theme.css", [css]),
            part("logo.png", [valid_binary("png")]),
            part("screen.woff2", [valid_binary("woff2")]),
            part("data.csv", [valid_csv()]),
        ]
    )
    try:
        assert staged.referenced_artifact_names == frozenset(
            {"theme.css", "logo.png", "screen.woff2", "data.csv"}
        )
        assert staged.referenced_csv_names == frozenset({"data.csv"})
    finally:
        staged.close()


def test_url_encoded_space_resolves_to_a_flat_supporting_filename() -> None:
    staged = stage_upload(
        [
            part(
                "dashboard.html",
                [valid_html(b'<script>fetch("sales%20data.csv")</script>')],
            ),
            part("sales data.csv", [valid_csv()]),
        ]
    )
    try:
        assert staged.referenced_artifact_names == frozenset({"sales data.csv"})
    finally:
        staged.close()


@pytest.mark.parametrize(
    "expression",
    [b"fetch(`data.csv`)", b"d3.csv(`data.csv`)", b"fetch(`data.csv`, {cache: 'no-store'})"],
)
def test_static_template_literal_resolves_to_a_local_csv(expression: bytes) -> None:
    staged = stage_upload(
        [
            part("dashboard.html", [valid_html(b"<script>" + expression + b"</script>")]),
            part("data.csv", [valid_csv()]),
        ]
    )
    try:
        assert staged.referenced_csv_names == frozenset({"data.csv"})
    finally:
        staged.close()


@pytest.mark.parametrize(
    "expression",
    [
        b"fetch(`./${csvStem}.csv`)",
        b"d3.csv(`${csvStem}.csv`)",
        b"fetch(`./${csvStem}.csv?cache=${Date.now()}`)",
    ],
)
def test_interpolated_template_data_path_is_runtime_scoped(expression: bytes) -> None:
    staged = stage_upload(
        [
            part("dashboard.html", [valid_html(b"<script>" + expression + b"</script>")]),
            part("data.csv", [valid_csv()]),
        ]
    )
    try:
        assert staged.referenced_csv_names == frozenset()
    finally:
        staged.close()


@pytest.mark.parametrize(
    "body",
    [
        b'<link rel="stylesheet" href="missing.css">',
        b'<link rel="stylesheet" href="theme.css?cache=1">',
        b'<link rel="stylesheet" href="../theme.css">',
        b'<img src="missing.png">',
        b'<img src="logo.png#fragment">',
        b'<img src="https://cdn.example/logo.png">',
        b'<img src="logo\\.png">',
        b'<style>body{background:url("missing.png")}</style>',
        b'<style>body{background:url("../logo.png")}</style>',
        b'<style>body{background:url("%2e%2e%2flogo.png")}</style>',
        b'<style>body{background:url("%252e%252e%252flogo.png")}</style>',
        b'<style>body{background:url("font.css")}</style>',
    ],
)
def test_undeclared_external_and_wrong_kind_asset_references_are_rejected(body: bytes) -> None:
    error = rejection(
        [
            part("dashboard.html", [valid_html(body)]),
            part("theme.css", [b"body{}"]),
            part("logo.png", [valid_binary("png")]),
            part("data.csv", [valid_csv()]),
        ]
    )
    assert error.issue.code in {
        UploadIssueCode.EXTERNAL_DEPENDENCY,
        UploadIssueCode.INVALID_ASSET_REFERENCE,
        UploadIssueCode.MISSING_ASSET_REFERENCE,
    }


def test_css_and_html_safe_data_blob_and_anchor_references_remain_allowed() -> None:
    body = (
        b'<a href="#section">anchor</a>'
        b'<img src="data:image/png;base64,AA==">'
        b'<img src="blob:opaque">'
        b"<style>body{background:url(data:image/png;base64,AA==)}</style>"
    )
    staged = stage_upload([part("dashboard.html", [valid_html(body)])])
    try:
        assert staged.referenced_artifact_names == frozenset()
    finally:
        staged.close()


def test_css_escapes_resolve_local_assets_but_cannot_hide_external_dependencies() -> None:
    staged = stage_upload(
        [
            part(
                "dashboard.html",
                [valid_html(rb'<style>body{background:url("l\6f go.png")}</style>')],
            ),
            part("logo.png", [valid_binary("png")]),
        ]
    )
    try:
        assert staged.referenced_artifact_names == frozenset({"logo.png"})
    finally:
        staged.close()

    for body in (
        rb'<style>@\69mport url("theme.css")</style>',
        rb"<style>body{background:u\72l(h\74tps://evil.example/x.png)}</style>",
        rb"<style>body{src:url(data:font/ttf;base64,AA==)}</style>",
    ):
        error = rejection([part("dashboard.html", [valid_html(body)])])
        assert error.issue.code == UploadIssueCode.EXTERNAL_DEPENDENCY


@pytest.mark.parametrize("expression", [b"d3.csv(url)", b"d3.csv()", b"d3 . csv(source)"])
def test_d3_csv_must_use_a_literal_local_csv_reference(expression: bytes) -> None:
    error = rejection(
        [
            part("dashboard.html", [valid_html(b"<script>" + expression + b"</script>")]),
            part("data.csv", [valid_csv()]),
        ]
    )
    assert error.issue.code == UploadIssueCode.EXTERNAL_DEPENDENCY


@pytest.mark.parametrize(
    ("body", "expected_field"),
    [
        (
            b'<script src="https://cdn.example/chart.js"></script>',
            UploadIssueField.HTML_SCRIPT_SOURCE,
        ),
        (
            b'<style>@import "theme.css";</style>',
            UploadIssueField.CSS_IMPORT,
        ),
        (
            b'<script>const source = "https://example.test/data";</script>',
            UploadIssueField.INLINE_SCRIPT_URL,
        ),
        (
            b"<script>new XMLHttpRequest()</script>",
            UploadIssueField.INLINE_NETWORK_API,
        ),
        (
            b"<script>fetch(csvName)</script>",
            UploadIssueField.DATA_IDENTIFIER,
        ),
        (
            b"<script>fetch(`data.csv)</script>",
            UploadIssueField.DATA_TEMPLATE_LITERAL,
        ),
        (
            b"<script>fetch(prefix + '.csv')</script>",
            UploadIssueField.DATA_IDENTIFIER,
        ),
    ],
)
def test_external_dependency_reports_safe_structural_field(
    body: bytes, expected_field: UploadIssueField
) -> None:
    error = rejection([part("dashboard.html", [valid_html(body)])])

    assert error.issue.code == UploadIssueCode.EXTERNAL_DEPENDENCY
    assert error.issue.field == expected_field


@pytest.mark.parametrize("declared_length", [None, 0, 1, 10_000_000])
def test_content_length_is_advisory_and_actual_chunks_are_authoritative(
    declared_length: int | None,
) -> None:
    content = valid_html()
    staged = stage_upload(
        [
            part(
                "dashboard.html",
                [content[:4], b"", content[4:]],
                media_type="text/html",
                content_length=declared_length,
            )
        ]
    )
    staged.close()


def test_limit_and_limit_plus_one_are_enforced_from_streamed_bytes() -> None:
    content = valid_html()
    exact = stage_upload(
        [part("dashboard.html", [content], media_type="text/html")],
        limits=UploadLimits(max_file_bytes=len(content), max_total_bytes=len(content)),
    )
    exact.close()

    error = rejection(
        [part("dashboard.html", [content[:-1], content[-1:]], media_type="text/html")],
        limits=UploadLimits(max_file_bytes=len(content) - 1, max_total_bytes=len(content)),
    )
    assert error.issue.code == UploadIssueCode.FILE_TOO_LARGE

    csv_content = valid_csv()
    error = rejection(
        [
            part("dashboard.html", [content], media_type="text/html"),
            part("data.csv", [csv_content], media_type="text/csv"),
        ],
        limits=UploadLimits(
            max_file_bytes=max(len(content), len(csv_content)),
            max_total_bytes=len(content) + len(csv_content) - 1,
        ),
    )
    assert error.issue.code == UploadIssueCode.TOTAL_TOO_LARGE


def test_file_count_limit_includes_the_required_html_part() -> None:
    csv_content = valid_csv()
    staged = stage_upload(
        [
            part("dashboard.html", [valid_html()], media_type="text/html"),
            part("one.csv", [csv_content], media_type="text/csv"),
        ],
        limits=UploadLimits(max_files=2),
    )
    staged.close()

    error = rejection(
        [
            part("dashboard.html", [valid_html()], media_type="text/html"),
            part("one.csv", [csv_content], media_type="text/csv"),
            part("two.csv", [csv_content], media_type="text/csv"),
        ],
        limits=UploadLimits(max_files=2),
    )
    assert error.issue.code == UploadIssueCode.TOO_MANY_FILES


def test_too_many_empty_chunks_are_bounded() -> None:
    error = rejection(
        [part("dashboard.html", [b""] * 3 + [valid_html()], media_type="text/html")],
        limits=UploadLimits(max_chunks_per_file=3),
    )
    assert error.issue.code == UploadIssueCode.TOO_MANY_CHUNKS


@pytest.mark.parametrize(
    "filename",
    [
        "",
        ".html",
        "../dashboard.html",
        "folder\\dashboard.html",
        "C:dashboard.html",
        "dashboard.html ",
        "dashboard.html.",
        "CON.html",
        "\uff23\uff2f\uff2e.html",
        "con .html",
        "dashboard.html.exe",
        "data.csv.html",
        "dashboard.txt",
        "dashboard",
    ],
)
def test_filename_policy_rejects_traversal_reserved_and_misleading_forms(filename: str) -> None:
    error = rejection([part(filename, [valid_html()], media_type="text/html")])
    assert error.issue.code in {
        UploadIssueCode.INVALID_FILENAME,
        UploadIssueCode.EXTENSION_MISMATCH,
    }


def test_multipart_filename_bytes_are_strictly_decoded() -> None:
    error = rejection([part(b"bad\xff.html", [valid_html()], media_type="text/html")])
    assert error.issue.code == UploadIssueCode.INVALID_FILENAME


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("café.csv", "café.csv"),
        ("Report.csv", "report.CSV"),
        ("\uff2b.csv", "k.csv"),
        ("Straße.csv", "STRASSE.csv"),
    ],
)
def test_filename_normalization_collisions_are_rejected_before_streams(
    first: str, second: str
) -> None:
    consumed = False

    def should_not_be_read() -> Iterator[bytes]:
        nonlocal consumed
        consumed = True
        yield valid_csv()

    error = rejection(
        [
            part("dashboard.html", [valid_html()], media_type="text/html"),
            part(first, [valid_csv()], media_type="text/csv"),
            part(second, should_not_be_read(), media_type="text/csv"),
        ]
    )
    assert error.issue.code == UploadIssueCode.DUPLICATE_FILENAME
    assert consumed is False


@pytest.mark.parametrize(
    "media_type",
    ["text/plain", "application/octet-stream", "text/html; charset=latin-1", ""],
)
def test_declared_media_type_must_match_extension_and_utf8_policy(media_type: str) -> None:
    error = rejection([part("dashboard.html", [valid_html()], media_type=media_type)])
    assert error.issue.code == UploadIssueCode.MEDIA_TYPE_MISMATCH


def test_missing_declared_media_type_and_case_insensitive_mime_are_supported() -> None:
    staged = stage_upload([part("dashboard.HTML", [valid_html()], media_type=None)])
    staged.close()
    staged = stage_upload(
        [
            part("dashboard.html", [valid_html()]),
            part("data.CSV", [valid_csv()], media_type="TEXT/CSV"),
        ]
    )
    staged.close()


@pytest.mark.parametrize("content", [b"", b"\n", b"\x00", b"\x01"])
def test_empty_and_binary_files_are_rejected(content: bytes) -> None:
    error = rejection([part("dashboard.html", [content], media_type="text/html")])
    assert error.issue.code in {
        UploadIssueCode.EMPTY_FILE,
        UploadIssueCode.BINARY_CONTENT,
        UploadIssueCode.HTML_MALFORMED,
    }


@pytest.mark.parametrize("content", [b"\xff<html></html>", b"\xff\xfe<\x00h\x00t\x00m\x00l\x00>"])
def test_non_utf8_html_is_rejected_without_replacement_decoding(content: bytes) -> None:
    error = rejection([part("dashboard.html", [content], media_type="text/html")])
    assert error.issue.code == UploadIssueCode.INVALID_UTF8


def test_html_content_is_detected_independently_of_declared_type() -> None:
    error = rejection([part("dashboard.html", [b"name,value\n1,2\n"], media_type="text/html")])
    assert error.issue.code == UploadIssueCode.HTML_MALFORMED
    error = rejection(
        [
            part("dashboard.html", [valid_html()]),
            part("data.csv", [b"<html></html>"], media_type="text/csv"),
        ]
    )
    assert error.issue.code == UploadIssueCode.MEDIA_TYPE_MISMATCH


@pytest.mark.parametrize(
    "content",
    [
        b'a,b\n"unterminated,b\n',
        b'a,"quoted"x\n',
        b'a,b\n"c\nd\n',
    ],
)
def test_csv_strict_structure_rejects_malformed_quotes(content: bytes) -> None:
    error = rejection(
        [
            part("dashboard.html", [valid_html()], media_type="text/html"),
            part("data.csv", [content], media_type="text/csv"),
        ]
    )
    assert error.issue.code == UploadIssueCode.CSV_MALFORMED


def test_csv_allows_quoted_newlines_and_preserves_formula_bytes() -> None:
    content = b'name,value\n"A\nB","=SUM(1,2)"\n'
    staged = stage_upload([part("dashboard.html", [valid_html()]), part("data.csv", [content])])
    try:
        assert b"".join(staged.files[1].iter_chunks()) == content
    finally:
        staged.close()


def test_csv_ragged_rows_are_rejected_as_non_tabular_structure() -> None:
    error = rejection([part("dashboard.html", [valid_html()]), part("data.csv", [b"a,b\n1\n"])])
    assert error.issue.code == UploadIssueCode.CSV_MALFORMED


def test_csv_limit_boundaries_cover_rows_fields_field_bytes_and_records() -> None:
    content = b"a,b\n1,2\n"
    exact = stage_upload(
        [part("dashboard.html", [valid_html()]), part("data.csv", [content])],
        limits=UploadLimits(
            max_csv_rows=2,
            max_csv_fields=2,
            max_csv_field_bytes=1,
            max_csv_record_bytes=4,
        ),
    )
    exact.close()

    cases = [
        (UploadLimits(max_csv_rows=1), UploadIssueCode.CSV_TOO_MANY_ROWS),
        (UploadLimits(max_csv_fields=1), UploadIssueCode.CSV_TOO_MANY_FIELDS),
        (UploadLimits(max_csv_record_bytes=3), UploadIssueCode.CSV_RECORD_TOO_LARGE),
    ]
    for limits, expected in cases:
        error = rejection(
            [part("dashboard.html", [valid_html()]), part("data.csv", [content])],
            limits=limits,
        )
        assert error.issue.code == expected
    error = rejection(
        [
            part("dashboard.html", [valid_html()]),
            part("data.csv", [b"aa,b\n1,2\n"]),
        ],
        limits=UploadLimits(max_csv_field_bytes=1),
    )
    assert error.issue.code == UploadIssueCode.CSV_FIELD_TOO_LARGE


def test_csv_blank_only_content_is_empty_and_html_looking_content_is_misleading() -> None:
    error = rejection([part("dashboard.html", [valid_html()]), part("data.csv", [b"\n\n"])])
    assert error.issue.code == UploadIssueCode.EMPTY_FILE
    error = rejection(
        [part("dashboard.html", [valid_html()]), part("data.csv", [b"<!doctype html>\n"])]
    )
    assert error.issue.code == UploadIssueCode.MEDIA_TYPE_MISMATCH


@pytest.mark.parametrize(
    "body",
    [
        b'<script src="https://cdn.example/app.js"></script>',
        b'<link rel="stylesheet" href="theme.css">',
        b'<img src="https://cdn.example/pixel.gif">',
        b'<iframe src="data:text/html,evil"></iframe>',
        b'<object data="report.pdf"></object>',
        b'<form action="https://evil.example"></form>',
        b'<base href="https://evil.example/">',
        b'<meta http-equiv="refresh" content="0;url=https://evil.example">',
        b'<style>@import "https://evil.example/style.css";</style>',
        b'<style>body { background: url("theme.png"); }</style>',
        b'<script>import "./library.js"</script>',
        b'<script>new Worker("worker.js")</script>',
        b'<script>fetch("https://evil.example/data.csv")</script>',
        b"<script>fetch(`https://evil.example/data.csv`)</script>",
        b"<script>fetch(url)</script>",
    ],
)
def test_html_rejects_external_or_unapproved_runtime_dependencies(body: bytes) -> None:
    error = rejection([part("dashboard.html", [valid_html(body)])])
    assert error.issue.code in {
        UploadIssueCode.EXTERNAL_DEPENDENCY,
        UploadIssueCode.INVALID_ASSET_REFERENCE,
        UploadIssueCode.MISSING_ASSET_REFERENCE,
    }


def test_html_allows_inline_css_js_safe_media_and_revision_csv_references() -> None:
    body = (
        b'<style>body{background:url("data:image/png;base64,AA==")}</style>'
        b'<img src="data:image/png;base64,AA==">'
        b'<a href="#section">anchor</a>'
        b'<script>if (a < b) fetch("./data.csv")</script>'
    )
    staged = stage_upload(
        [part("dashboard.html", [valid_html(body)]), part("data.csv", [valid_csv()])]
    )
    try:
        assert staged.referenced_csv_names == frozenset({"data.csv"})
    finally:
        staged.close()


def test_html_allows_library_text_that_only_mentions_import_words() -> None:
    body = (
        b"<script>"
        b"const important = true;"
        b"const message = 'Please import and register the plugin';"
        b"</script>"
    )
    staged = stage_upload([part("dashboard.html", [valid_html(body)])])
    staged.close()


def test_large_inline_script_is_validated_independently_of_chunk_boundaries() -> None:
    content = valid_html(b"<script>" + (b"void 0;\n" * 20_000) + b"</script>")
    chunks = [content[offset : offset + 64 * 1024] for offset in range(0, len(content), 64 * 1024)]

    staged = stage_upload([part("dashboard.html", chunks)])

    try:
        assert staged.files[0].byte_size == len(content)
        assert b"".join(staged.files[0].iter_chunks()) == content
    finally:
        staged.close()


@pytest.mark.parametrize(
    "reference",
    [
        "missing.csv",
        "../data.csv",
        "/data.csv",
        "./nested/data.csv",
        "data.csv?download=1",
        "data.csv#fragment",
        "%2e%2e/data.csv",
        "data\\.csv",
        "data.txt",
        "https://portal.example/data.csv",
    ],
)
def test_html_csv_references_are_revision_relative_and_declared(reference: str) -> None:
    body = f'<script>fetch("{reference}")</script>'.encode()
    error = rejection([part("dashboard.html", [valid_html(body)]), part("data.csv", [valid_csv()])])
    assert error.issue.code in {
        UploadIssueCode.MISSING_CSV_REFERENCE,
        UploadIssueCode.INVALID_CSV_REFERENCE,
        UploadIssueCode.EXTERNAL_DEPENDENCY,
    }


@pytest.mark.parametrize(
    "content",
    [
        b"<html><body>x</html>",
        b"<body>x</body>",
        b"text<html></html>",
        b"<html><body><div></body></html>",
        b"<html><div/ ></html>",
        b"<html><div a='x' a='y'></div></html>",
        b"<html></html>tail",
        b"<html></html><!-- never closed",
        b"<!doctype svg><html></html>",
    ],
)
def test_html_structure_is_checked_strictly(content: bytes) -> None:
    error = rejection([part("dashboard.html", [content])])
    assert error.issue.code == UploadIssueCode.HTML_MALFORMED


def test_html_work_limits_are_bounded() -> None:
    nested = b"<html>" + b"<div>" * 3 + b"x" + b"</div>" * 3 + b"</html>"
    error = rejection([part("dashboard.html", [nested])], limits=UploadLimits(max_html_nesting=3))
    assert error.issue.code == UploadIssueCode.HTML_TOO_COMPLEX
    error = rejection(
        [part("dashboard.html", [valid_html()])], limits=UploadLimits(max_html_tokens=2)
    )
    assert error.issue.code == UploadIssueCode.HTML_TOO_COMPLEX


def test_stream_disconnect_and_non_bytes_chunks_are_typed_and_cleaned() -> None:
    def disconnects() -> Iterator[bytes]:
        yield valid_html()[:7]
        raise ConnectionError("raw stream details must not escape")

    error = rejection([part("dashboard.html", disconnects())])
    assert error.issue.code == UploadIssueCode.STREAM_ERROR
    assert "raw stream" not in str(error)

    error = rejection([part("dashboard.html", [b"not-bytes"])])
    assert error.issue.code == UploadIssueCode.HTML_MALFORMED
    error = rejection(
        [
            part(
                "dashboard.html",
                cast(Iterable[bytes], [valid_html()[:4], "not bytes"]),
            )
        ]
    )
    assert error.issue.code == UploadIssueCode.NON_BYTES_CHUNK


def test_invalid_multipart_and_configuration_are_rejected() -> None:
    error = rejection(cast_parts())
    assert error.issue.code == UploadIssueCode.MALFORMED_MULTIPART
    with pytest.raises(ValueError, match="decimal"):
        UploadLimits.from_mapping({"AGORA_UPLOAD_MAX_FILES": "many"})
    with pytest.raises(ValueError, match="nonnegative"):
        UploadLimits(max_file_bytes=-1)
    with pytest.raises(ValueError, match="include the HTML"):
        UploadLimits(max_files=0)


def test_limits_mapping_properties_and_advisory_media_type_are_typed() -> None:
    limits = UploadLimits.from_mapping(
        {
            "AGORA_UPLOAD_MAX_FILE_BYTES": "10",
            "AGORA_UPLOAD_MAX_TOTAL_BYTES": "20",
            "AGORA_UPLOAD_MAX_FILES": "3",
            "AGORA_UPLOAD_MAX_CSV_ROWS": "4",
            "AGORA_UPLOAD_MAX_CSV_FIELDS": "5",
            "AGORA_UPLOAD_MAX_CSV_FIELD_BYTES": "6",
            "AGORA_UPLOAD_MAX_CSV_RECORD_BYTES": "7",
            "AGORA_UPLOAD_MAX_HTML_TOKENS": "8",
            "AGORA_UPLOAD_MAX_HTML_NESTING": "9",
            "AGORA_UPLOAD_MAX_CHUNKS_PER_FILE": "10",
        }
    )
    assert limits.max_csv_files == 2
    assert limits.max_supporting_files == 2
    assert limits.max_file_bytes == 10
    assert (
        part("dashboard.html", [valid_html()], media_type="text/html").declared_media_type
        == "text/html"
    )
    for field in (
        "max_csv_rows",
        "max_csv_fields",
        "max_csv_field_bytes",
        "max_csv_record_bytes",
        "max_html_tokens",
        "max_html_nesting",
        "max_chunks_per_file",
    ):
        with pytest.raises(ValueError, match=field):
            UploadLimits(**{field: 0})


def test_staged_upload_context_and_read_failures_are_cleanup_safe() -> None:
    staged = stage_upload([part("dashboard.html", [valid_html()])])
    assert staged.artifacts == staged.files
    with staged as entered:
        assert entered is staged
    staged.close()
    with pytest.raises(ValueError, match="chunk size"):
        list(staged.files[0].iter_chunks(0))

    class ReadFailure:
        def seek(self, offset: int) -> None:
            del offset

        def read(self, size: int) -> bytes:
            del size
            raise RuntimeError("read failure")

    file = StagedUploadFile(
        UploadKind.HTML,
        normalize_logical_name("dashboard.html"),
        1,
        "0" * 64,
        cast(BinaryIO, ReadFailure()),
    )
    with pytest.raises(OSError, match="read staged"):
        list(file.iter_chunks())

    class NonBytesRead:
        def seek(self, offset: int) -> None:
            del offset

        def read(self, size: int) -> str:
            del size
            return "not bytes"

    file = StagedUploadFile(
        UploadKind.HTML,
        normalize_logical_name("dashboard.html"),
        1,
        "0" * 64,
        cast(BinaryIO, NonBytesRead()),
    )
    with pytest.raises(OSError, match="read staged"):
        list(file.iter_chunks())

    class EmptyNonBytesRead:
        def seek(self, offset: int) -> None:
            del offset

        def read(self, size: int) -> str:
            del size
            return ""

    file = StagedUploadFile(
        UploadKind.HTML,
        normalize_logical_name("dashboard.html"),
        1,
        "0" * 64,
        cast(BinaryIO, EmptyNonBytesRead()),
    )
    with pytest.raises(OSError, match="read staged"):
        list(file.iter_chunks())


def test_manifest_and_stream_setup_failures_are_reported_before_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(UploadRejected) as raised:
        stage_upload([cast(UploadPart, object())])
    assert raised.value.issue.code == UploadIssueCode.MALFORMED_MULTIPART

    def failing_parts() -> Iterator[UploadPart]:
        yield part("dashboard.html", [valid_html()])
        raise RuntimeError("multipart iterator failed")

    with pytest.raises(UploadRejected) as raised:
        stage_upload(failing_parts())
    assert raised.value.issue.code == UploadIssueCode.MALFORMED_MULTIPART

    with pytest.raises(UploadRejected) as raised:
        stage_upload([part("data.csv", [valid_csv()])])
    assert raised.value.issue.code == UploadIssueCode.MISSING_HTML
    with pytest.raises(UploadRejected) as raised:
        stage_upload([part("one.html", [valid_html()]), part("two.HTML", [valid_html()])])
    assert raised.value.issue.code == UploadIssueCode.MULTIPLE_HTML
    with pytest.raises(UploadRejected) as raised:
        stage_upload([part(cast(str | bytes, 1), [valid_html()])])
    assert raised.value.issue.code == UploadIssueCode.INVALID_FILENAME

    with pytest.raises(UploadRejected) as raised:
        stage_upload([part("dashboard.html", [valid_html()], media_type="text/html;")])
    assert raised.value.issue.code == UploadIssueCode.MEDIA_TYPE_MISMATCH

    monkeypatch.setattr(
        "agora.uploads.tempfile.TemporaryFile",
        lambda **kwargs: (_ for _ in ()).throw(OSError("no temp")),
    )
    with pytest.raises(UploadRejected) as raised:
        stage_upload([part("dashboard.html", [valid_html()])])
    assert raised.value.issue.code == UploadIssueCode.STAGING_ERROR


def test_callable_sources_write_progress_and_bom_boundaries_are_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def source() -> Iterator[bytes]:
        yield valid_html()

    staged = stage_upload([part("dashboard.html", source)])
    staged.close()

    def failing_source() -> Iterator[bytes]:
        raise OSError("source could not open")

    error = rejection([part("dashboard.html", failing_source)])
    assert error.issue.code == UploadIssueCode.STREAM_ERROR

    class NoProgress:
        def write(self, value: object) -> int:
            del value
            return 0

        def close(self) -> None:
            pass

    monkeypatch.setattr("agora.uploads.tempfile.TemporaryFile", lambda **kwargs: NoProgress())
    error = rejection([part("dashboard.html", [valid_html()])])
    assert error.issue.code == UploadIssueCode.STAGING_ERROR


def test_html_attribute_policy_and_reference_edge_cases() -> None:
    valid_body = (
        b'<style style="color:red">body{color:red}</style>'
        b'<div style="color:blue" onclick="alert(1)">x</div>'
        b'<img src="blob:opaque"><br/><meta charset="utf-8">'
        b"<!-- closed -->"
        b'<a href="#local">local</a>'
    )
    staged = stage_upload([part("dashboard.html", [valid_html(valid_body)])])
    staged.close()

    invalid_bodies = [
        b'<meta http-equiv="refresh" content="0">',
        b'<meta name="x" content="https://evil.example">',
        b'<img src="">',
        b'<a href="  ">bad</a>',
        b'<a href="dashboard.html">entry point is not a supporting file</a>',
        b'<video src="data.csv"></video>',
        b'<img srcset="data:image/png;base64,AA==">',
        b'<html manifest="manifest.appcache"></html>',
        b'<a ping="https://evil.example">x</a>',
        b'<script>fetch("data.csv" + secret)</script>',
        b'<script>d3.csv("missing.csv")</script>',
    ]
    for body in invalid_bodies:
        error = rejection(
            [part("dashboard.html", [valid_html(body)]), part("data.csv", [valid_csv()])]
        )
        assert error.issue.code in {
            UploadIssueCode.EXTERNAL_DEPENDENCY,
            UploadIssueCode.INVALID_CSV_REFERENCE,
            UploadIssueCode.MISSING_CSV_REFERENCE,
            UploadIssueCode.INVALID_ASSET_REFERENCE,
            UploadIssueCode.MISSING_ASSET_REFERENCE,
            UploadIssueCode.HTML_MALFORMED,
        }

    error = rejection(
        [
            part("dashboard.html", [valid_html(b'<script>fetch("//[")</script>')]),
            part("data.csv", [valid_csv()]),
        ]
    )
    assert error.issue.code == UploadIssueCode.INVALID_CSV_REFERENCE
    error = rejection(
        [
            part("dashboard.html", [valid_html(b'<script>fetch("CON.csv")</script>')]),
            part("data.csv", [valid_csv()]),
        ]
    )
    assert error.issue.code == UploadIssueCode.INVALID_CSV_REFERENCE


def cast_parts() -> Iterable[UploadPart]:
    class BrokenParts:
        def __iter__(self) -> Iterator[UploadPart]:
            raise RuntimeError("multipart parser failed")

    return BrokenParts()


@pytest.mark.django_db(transaction=True)
def test_persistence_adapter_commits_only_after_validation_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    owner = User.objects.create_user("UPLOAD.OWNER")
    dashboard = Dashboard.objects.create(owner=owner, name="Upload test")
    storage = FilesystemArtifactStorage(tmp_path / "private")
    html = valid_html(b'<script>fetch("data.csv")</script>')
    csv_content = valid_csv(b"=1+1")
    revision = create_upload_revision(
        dashboard_id=dashboard.id,
        created_by_id=owner.id,
        parts=[part("dashboard.html", [html]), part("data.csv", [csv_content])],
        storage=storage,
    )

    assert revision.number == 1
    assert Revision.objects.filter(id=revision.id).count() == 1
    assert AuditEvent.objects.get().event_type == "revision.created"
    assert StorageReservation.objects.count() == 0
    for artifact in revision.artifacts.all():
        with storage.open(persistence_key(artifact.storage_key)) as stream:
            expected = html if artifact.kind == Artifact.Kind.HTML else csv_content
            assert stream.read() == expected


def persistence_key(value: str) -> StorageKey:
    return StorageKey(value)


@pytest.mark.django_db(transaction=True)
def test_persistence_adapter_validation_failure_does_not_touch_revision_or_storage(
    tmp_path: Path,
) -> None:
    owner = User.objects.create_user("UPLOAD.FAIL")
    dashboard = Dashboard.objects.create(owner=owner, name="Upload failure")
    storage = FilesystemArtifactStorage(tmp_path / "private")
    error = rejection(
        [part("dashboard.html", [valid_html()]), part("data.csv", [b'broken,"quote'])]
    )
    assert error.issue.code == UploadIssueCode.CSV_MALFORMED
    with pytest.raises(UploadRejected):
        create_upload_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            parts=[part("dashboard.html", [valid_html()]), part("data.csv", [b'broken,"quote'])],
            storage=storage,
        )
    assert Revision.objects.filter(dashboard=dashboard).count() == 0
    assert StorageReservation.objects.count() == 0
    assert [path for path in storage._root.rglob("*") if path.is_file()] == []


@pytest.mark.django_db(transaction=True)
def test_persistence_metadata_failure_cleans_completed_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = User.objects.create_user("UPLOAD.META.FAIL")
    dashboard = Dashboard.objects.create(owner=owner, name="Metadata failure")
    storage = FilesystemArtifactStorage(tmp_path / "private")

    def fail_commit(**kwargs: object) -> Revision:
        del kwargs
        raise IntegrityError("simulated metadata failure")

    monkeypatch.setattr(persistence_services, "_commit_revision_metadata", fail_commit)
    with pytest.raises(IntegrityError, match="metadata failure"):
        create_upload_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            parts=[part("dashboard.html", [valid_html()]), part("data.csv", [valid_csv()])],
            storage=storage,
        )
    assert Revision.objects.filter(dashboard=dashboard).count() == 0
    assert StorageReservation.objects.count() == 0
    assert [path for path in storage._root.rglob("*") if path.is_file()] == []


class WriteFailingStorage(FilesystemArtifactStorage):
    def write(self, *args: object, **kwargs: object) -> StoredArtifact:
        del args, kwargs
        raise ArtifactStorageError("simulated write failure")


class CommittedThenFailedStorage(FilesystemArtifactStorage):
    def write(
        self,
        key: StorageKey,
        chunks: Iterable[bytes],
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredArtifact:
        receipt = super().write(
            key,
            chunks,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        raise StorageWriteCommitted(receipt)


class UncertainWriteStorage(FilesystemArtifactStorage):
    def write(
        self,
        key: StorageKey,
        chunks: Iterable[bytes],
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredArtifact:
        del key, chunks, expected_size, expected_sha256
        raise StorageCleanupRequired("simulated uncertain cleanup")


@pytest.mark.django_db(transaction=True)
def test_persistence_write_failure_leaves_no_visible_revision_or_reservation(
    tmp_path: Path,
) -> None:
    owner = User.objects.create_user("UPLOAD.WRITE.FAIL")
    dashboard = Dashboard.objects.create(owner=owner, name="Write failure")
    storage = WriteFailingStorage(tmp_path / "private")
    with pytest.raises(ArtifactStorageError, match="write failure"):
        create_upload_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            parts=[part("dashboard.html", [valid_html()])],
            storage=storage,
        )
    assert Revision.objects.filter(dashboard=dashboard).count() == 0
    assert StorageReservation.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_persistence_committed_write_failure_is_cleaned_before_exposure(tmp_path: Path) -> None:
    owner = User.objects.create_user("UPLOAD.COMMITTED.FAIL")
    dashboard = Dashboard.objects.create(owner=owner, name="Committed write failure")
    storage = CommittedThenFailedStorage(tmp_path / "private")
    with pytest.raises(StorageWriteCommitted, match="committed"):
        create_upload_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            parts=[part("dashboard.html", [valid_html()])],
            storage=storage,
        )

    assert Revision.objects.filter(dashboard=dashboard).count() == 0
    assert StorageReservation.objects.count() == 0
    assert [path for path in storage._root.rglob("*") if path.is_file()] == []


@pytest.mark.django_db(transaction=True)
def test_persistence_uncertain_write_retains_cleanup_marker_without_revision(
    tmp_path: Path,
) -> None:
    owner = User.objects.create_user("UPLOAD.UNCERTAIN.FAIL")
    dashboard = Dashboard.objects.create(owner=owner, name="Uncertain write failure")
    storage = UncertainWriteStorage(tmp_path / "private")
    with pytest.raises(StorageCleanupRequired, match="uncertain"):
        create_upload_revision(
            dashboard_id=dashboard.id,
            created_by_id=owner.id,
            parts=[part("dashboard.html", [valid_html()])],
            storage=storage,
        )

    assert Revision.objects.filter(dashboard=dashboard).count() == 0
    reservation = StorageReservation.objects.get()
    assert reservation.cleanup_required is True
