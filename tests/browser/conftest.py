from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest

from .server import BrowserFixtureStack


@pytest.fixture(scope="session")
def browser_stack() -> Iterator[BrowserFixtureStack]:
    with BrowserFixtureStack() as stack:
        yield stack


@pytest.fixture(scope="session")
def browser_type_launch_args(
    browser_type_launch_args: dict[str, object],
    browser_name: str,
    browser_stack: BrowserFixtureStack,
) -> dict[str, object]:
    """Resolve the fixture hostnames to loopback without editing a developer hosts file."""
    if browser_name != "chromium":
        return browser_type_launch_args
    existing_args = cast(list[str], browser_type_launch_args.get("args", []))
    return {
        **browser_type_launch_args,
        "args": [
            *existing_args,
            f"--host-resolver-rules={browser_stack.host_resolver_rules}",
        ],
    }
