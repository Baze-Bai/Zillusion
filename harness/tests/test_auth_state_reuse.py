"""auth_state.json reuse — the bound store re-authenticates the main context on
(re)create.

This is the mechanism behind all three documented reuse points: the main
browser (`browser_attach` -> bind_auth_state -> _new_main_context), the
generated workflow.py, and the validator's re-fetch all create a Playwright
context with ``storage_state=<auth_state.json>``. Here we pin the main-browser
end: cookies present when the file exists, harmless no-op when it doesn't.
"""

from __future__ import annotations

import pytest

from mcp_server.browser import Browser

pytestmark = pytest.mark.browser


async def test_main_context_authenticated_when_auth_file_present(write_storage_state, make_cookie):
    auth = write_storage_state([make_cookie("sid", "xyz", "example.com")])
    b = Browser(headless=True)
    b.bind_auth_state(auth)
    await b.ensure_started()
    try:
        names = {c["name"] for c in await b.context.cookies()}
        assert "sid" in names  # main browser came up logged-in from the store
    finally:
        await b.shutdown()


async def test_main_context_clean_when_auth_file_absent(tmp_path):
    b = Browser(headless=True)
    b.bind_auth_state(tmp_path / "does_not_exist.json")  # bound but missing
    await b.ensure_started()
    try:
        assert await b.context.cookies() == []  # no-op until save_auth writes it
    finally:
        await b.shutdown()
