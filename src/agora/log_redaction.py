"""Logging filters for request targets that contain short-lived bearer credentials."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

_CONTENT_TOKEN_SEGMENT = re.compile(r"(/render/(?:preview|viewer)/)[^/?\s]+")


def redact_content_request_target(value: object) -> object:
    """Replace the credential segment while preserving a useful route shape."""
    if not isinstance(value, str):
        return value
    return _CONTENT_TOKEN_SEGMENT.sub(r"\1[REDACTED]", value)


class ContentRequestTargetFilter(logging.Filter):
    """Redact content credentials from message templates and structured arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_content_request_target(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_content_request_target(value) for value in record.args)
        elif isinstance(record.args, Mapping):
            record.args = {
                key: redact_content_request_target(value) for key, value in record.args.items()
            }
        return True


__all__ = ["ContentRequestTargetFilter", "redact_content_request_target"]
