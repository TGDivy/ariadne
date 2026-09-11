from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ariadne.browser import (
    BrowserApprovalError,
    BrowserLeaseError,
    BrowserReferenceError,
    BrowserStateError,
)
from ariadne.browser.runtime import BrowserRuntime
from ariadne.config import BrowserConfig

REQUIRE_BROWSER_ENVIRONMENT = "ARIADNE_REQUIRE_BROWSER_TESTS"


async def _launch_chromium_once() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await browser.close()


@pytest.fixture(scope="module", autouse=True)
def chromium_build() -> None:
    """Skip the Playwright-backed tests when Chromium is absent from this host.

    A host without the Chromium build should report an honest skip instead of an
    unrelated failure. Set ``ARIADNE_REQUIRE_BROWSER_TESTS=1`` wherever the browser
    really is provisioned so a missing build fails loudly.
    """
    try:
        asyncio.run(_launch_chromium_once())
    except Exception as error:
        if os.environ.get(REQUIRE_BROWSER_ENVIRONMENT, "") in {"1", "true"}:
            raise
        pytest.skip(
            "Playwright's Chromium build is unavailable "
            f"({type(error).__name__}). Run "
            "`uv run playwright install --with-deps chromium`, or set "
            f"{REQUIRE_BROWSER_ENVIRONMENT}=1 to make its absence a failure.",
            allow_module_level=True,
        )


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/download":
            body = b"fixture download\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain")
            self.send_header(
                "Content-Disposition", 'attachment; filename="fixture.txt"'
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/next":
            self._html("<title>Next page</title><main>Navigation complete</main>")
            return
        if self.path == "/login":
            self._html(
                """
                <title>Login required</title>
                <main>
                  <label>Password <input id="password" type="password"></label>
                  <label>Card <input id="card" autocomplete="cc-number"></label>
                </main>
                """
            )
            return
        if self.path == "/dynamic":
            self._html(
                """
                <title>Dynamic page</title>
                <main><button>Original action</button></main>
                <script>
                  setTimeout(() => {
                    const button = document.createElement('button');
                    button.textContent = 'Inserted action';
                    document.querySelector('main').prepend(button);
                  }, 200);
                </script>
                """
            )
            return
        if self.path == "/long":
            buttons = "".join(f"<button>Action {index}</button>" for index in range(50))
            self._html(
                "<title>Bounded page</title><main>4111 1111 1111 1111 "
                + ("private-looking content " * 200)
                + buttons
                + "</main>"
            )
            return
        if self.path == "/submit":
            self._html(
                """
                <title>Submission fixture</title>
                <main>
                  <p id="status">Prepared</p>
                  <label>Quantity <input value="1"></label>
                  <button
                    onclick="document.getElementById('status').textContent='Submitted'">
                    Place request
                  </button>
                </main>
                """
            )
            return
        self._html(
            """
            <title>Browser fixture</title>
            <main>
              <h1>Fixture shop</h1>
              <p id="status">Ready</p>
              <label>Name <input id="name" placeholder="Your name"></label>
              <label>Mode
                <select id="mode">
                  <option value="delivery">Delivery</option>
                  <option value="collection">Collection</option>
                </select>
              </label>
              <label>Artifact <input id="upload" type="file"></label>
              <button id="save"
                onclick="localStorage.saved='yes';
                  document.getElementById('status').textContent='Saved'">
                Save preference
              </button>
              <button id="dialog" onclick="alert('Confirm fixture')">
                Open dialog
              </button>
              <button id="permission" onclick="Notification.requestPermission()">
                Ask notification permission
              </button>
              <a href="/next">Next page</a>
              <a href="/download" download>Download fixture</a>
              <a href="/next" target="_blank">Open popup</a>
              <a href="mailto:fixture@example.invalid">Open mail app</a>
              <output id="persisted"></output>
              <script>
                persisted.textContent = localStorage.saved === 'yes'
                  ? 'Preference persisted' : 'No preference';
                upload.addEventListener(
                  'change', () =>
                    document.getElementById('status').textContent='File attached');
              </script>
            </main>
            """
        )

    def _html(self, value: str) -> None:
        body = value.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture(scope="module")
def fixture_site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def browser_config(tmp_path: Path) -> BrowserConfig:
    return BrowserConfig(
        enabled=True,
        state=tmp_path / "state" / "browser.sqlite3",
        profiles=tmp_path / "profiles",
        artifacts=tmp_path / "artifacts",
        socket=tmp_path / "run" / "browser.sock",
        headless=True,
        lease_seconds=60,
        action_timeout_seconds=5,
        observation_max_chars=2_000,
        observation_max_elements=30,
        journal_max_entries=100,
    )


def ref_named(observation: dict[str, object], name: str) -> str:
    elements = observation["elements"]
    assert isinstance(elements, list)
    return next(
        str(element["ref"])
        for element in elements
        if isinstance(element, dict) and element.get("name") == name
    )


async def test_fixture_navigation_forms_artifacts_and_takeover(
    tmp_path: Path, fixture_site: str
) -> None:
    runtime = BrowserRuntime(browser_config(tmp_path))
    await runtime.start()
    try:
        await runtime.create_profile("personal")
        started = await runtime.start_session("personal", "fixture-task")
        lease = started["lease"]
        token = lease["lease_token"]
        observation = await runtime.navigate("fixture-task", token, fixture_site)

        assert observation["title"] == "Browser fixture"
        assert observation["origin"] == fixture_site
        assert "Fixture shop" in observation["text"]
        assert observation["new_origin"] == fixture_site

        name_ref = ref_named(observation, "Name")
        typed = await runtime.type_text(
            "fixture-task", token, name_ref, "Ariadne fixture"
        )
        assert typed["page_revision"] > observation["page_revision"]
        with pytest.raises(BrowserReferenceError, match="stale"):
            await runtime.click("fixture-task", token, name_ref)

        mode_ref = ref_named(typed, "Mode")
        selected = await runtime.select("fixture-task", token, mode_ref, ["collection"])
        upload_ref = ref_named(selected, "Artifact")
        upload = tmp_path / "upload.txt"
        upload.write_text("fixture", encoding="utf-8")
        attached = await runtime.upload("fixture-task", token, upload_ref, str(upload))
        assert "File attached" in attached["text"]

        shot = await runtime.screenshot("fixture-task", token)
        shot_path = Path(shot["path"])
        assert shot_path.is_file()
        assert shot_path.stat().st_mode & 0o777 == 0o600

        after_screenshot = await runtime.inspect("fixture-task", token)
        dialog_ref = ref_named(after_screenshot, "Open dialog")
        dialog_result = await runtime.click("fixture-task", token, dialog_ref)
        assert dialog_result["events"] == [
            {
                "type": "dialog",
                "dialog_type": "alert",
                "message": "Confirm fixture",
                "disposition": "dismissed",
            }
        ]

        permission_ref = ref_named(dialog_result, "Ask notification permission")
        permission_result = await runtime.click("fixture-task", token, permission_ref)
        assert permission_result["events"] == [
            {
                "type": "browser_permission_requested",
                "permission": "notifications",
                "disposition": "not_auto_accepted",
            }
        ]

        download_ref = ref_named(permission_result, "Download fixture")
        downloaded = await runtime.download("fixture-task", token, download_ref)
        path = Path(downloaded["download"]["path"])
        assert path.read_text(encoding="utf-8") == "fixture download\n"
        assert downloaded["download"]["status"] == "inert_private_artifact"

        current = await runtime.inspect("fixture-task", token)
        external_ref = ref_named(current, "Open mail app")
        with pytest.raises(BrowserStateError, match="external application"):
            await runtime.click("fixture-task", token, external_ref)

        login = await runtime.navigate("fixture-task", token, f"{fixture_site}/login")
        assert login["task_state"] == "takeover"
        assert login["human_attention_required"]["reason"] == (
            "login_mfa_or_payment_field"
        )
        password_ref = ref_named(login, "Password")
        with pytest.raises(BrowserLeaseError):
            await runtime.type_text(
                "fixture-task", token, password_ref, "not-a-real-password"
            )
        await runtime._tasks["fixture-task"].page.goto(fixture_site)  # noqa: SLF001
        resumed = await runtime.resume_takeover("fixture-task", token)
        assert resumed["lease"]["state"] == "active"

        released = await runtime.release_session("fixture-task", token)
        assert released["lease"]["state"] == "released"
    finally:
        await runtime.close()


async def test_popup_tabs_and_persistent_profile_survive_runtime_restart(
    tmp_path: Path, fixture_site: str
) -> None:
    config = browser_config(tmp_path)
    first = BrowserRuntime(config)
    await first.start()
    try:
        await first.create_profile("personal")
        started = await first.start_session("personal", "first-task")
        token = started["lease"]["lease_token"]
        page = await first.navigate("first-task", token, fixture_site)
        save_ref = ref_named(page, "Save preference")
        saved = await first.click("first-task", token, save_ref)
        assert "Saved" in saved["text"]
        popup_ref = ref_named(saved, "Open popup")
        await first.click("first-task", token, popup_ref)
        tabs = await first.list_tabs("first-task", token)
        assert tabs["count"] == 2
        selected = await first.select_tab("first-task", token, "tab-1")
        assert selected["title"] == "Next page"
        await first.release_session("first-task", token)
    finally:
        await first.close()

    second = BrowserRuntime(config)
    await second.start()
    try:
        started = await second.start_session("personal", "second-task")
        token = started["lease"]["lease_token"]
        page = await second.navigate("second-task", token, fixture_site)
        assert "Preference persisted" in page["text"]
        await second.release_session("second-task", token)
    finally:
        await second.close()


async def test_external_dom_change_invalidates_an_inspected_reference(
    tmp_path: Path, fixture_site: str
) -> None:
    runtime = BrowserRuntime(browser_config(tmp_path))
    await runtime.start()
    try:
        await runtime.create_profile("personal")
        started = await runtime.start_session("personal", "dynamic-task")
        token = started["lease"]["lease_token"]
        observed = await runtime.navigate(
            "dynamic-task", token, f"{fixture_site}/dynamic"
        )
        original_ref = ref_named(observed, "Original action")
        await asyncio.sleep(0.3)

        with pytest.raises(BrowserReferenceError, match="page changed"):
            await runtime.click("dynamic-task", token, original_ref)
    finally:
        await runtime.close()


async def test_observation_is_bounded_and_redacts_payment_numbers(
    tmp_path: Path, fixture_site: str
) -> None:
    runtime = BrowserRuntime(browser_config(tmp_path))
    await runtime.start()
    try:
        await runtime.create_profile("personal")
        started = await runtime.start_session("personal", "bounded-task")
        token = started["lease"]["lease_token"]
        observed = await runtime.navigate("bounded-task", token, f"{fixture_site}/long")

        assert observed["text_truncated"] is True
        assert len(observed["text"]) <= 2_000
        assert "4111 1111 1111 1111" not in observed["text"]
        assert "[PAYMENT FIELD REDACTED]" in observed["text"]
        assert len(observed["elements"]) == 30
    finally:
        await runtime.close()


async def test_consequential_click_requires_bound_confirmation_and_replays(
    tmp_path: Path, fixture_site: str
) -> None:
    runtime = BrowserRuntime(browser_config(tmp_path))
    await runtime.start()
    try:
        await runtime.create_profile("personal")
        started = await runtime.start_session("personal", "submission-task")
        token = started["lease"]["lease_token"]
        observed = await runtime.navigate(
            "submission-task", token, f"{fixture_site}/submit"
        )
        submit_ref = ref_named(observed, "Place request")

        with pytest.raises(BrowserApprovalError, match="trusted approval"):
            await runtime.click(
                "submission-task",
                token,
                submit_ref,
                action_kind="consequential",
                operation_id="fixture-submit",
            )

        await runtime.record_confirmation(
            approval_id="stale-approval",
            task_id="submission-task",
            lease_token=token,
            page_revision=observed["page_revision"],
            digest=observed["state_digest"],
            ttl_seconds=60,
        )
        quantity_ref = ref_named(observed, "Quantity")
        changed = await runtime.type_text("submission-task", token, quantity_ref, "2")
        submit_ref = ref_named(changed, "Place request")
        with pytest.raises(BrowserApprovalError, match="state changed"):
            await runtime.click(
                "submission-task",
                token,
                submit_ref,
                action_kind="consequential",
                operation_id="stale-submit",
                approval_id="stale-approval",
            )

        await runtime.record_confirmation(
            approval_id="fixture-approval",
            task_id="submission-task",
            lease_token=token,
            page_revision=changed["page_revision"],
            digest=changed["state_digest"],
            ttl_seconds=60,
        )
        submitted = await runtime.click(
            "submission-task",
            token,
            submit_ref,
            action_kind="consequential",
            operation_id="fixture-submit",
            approval_id="fixture-approval",
        )
        assert "Submitted" in submitted["text"]

        current = await runtime.inspect("submission-task", token)
        replay_ref = ref_named(current, "Place request")
        replay = await runtime.click(
            "submission-task",
            token,
            replay_ref,
            action_kind="consequential",
            operation_id="fixture-submit",
            approval_id="fixture-approval",
        )
        assert replay == submitted
    finally:
        await runtime.close()
