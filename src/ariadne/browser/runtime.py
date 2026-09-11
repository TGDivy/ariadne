"""Long-lived Playwright runtime behind Ariadne's bounded browser protocol."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import (
    BrowserContext,
    Dialog,
    Download,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from ..config import BrowserConfig
from ..redaction import redact_sensitive_text
from .models import (
    ActionOutcome,
    BrowserApprovalError,
    BrowserConfigurationError,
    BrowserLeaseError,
    BrowserReferenceError,
    BrowserStateError,
    BrowserUncertainError,
)
from .state import BrowserState, state_digest

_INTERACTIVE_SELECTOR = (
    "a[href], button, input, select, textarea, [role], [contenteditable='true']"
)
_CARD_NUMBER = re.compile(r"(?<!\d)(?:\d[ -]*?){13,19}(?!\d)")
_SENSITIVE_FIELD = re.compile(
    r"(?:password|passcode|one.?time|otp|card|credit|debit|cvv|cvc|security.?code)",
    re.IGNORECASE,
)
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")

ActionKind = Literal["reversible", "consequential"]


@dataclass(slots=True)
class _OpenProfile:
    context: BrowserContext


@dataclass(slots=True)
class _TaskRuntime:
    profile_name: str
    page: Page
    references: dict[str, _ElementReference] = field(default_factory=dict)
    screenshot_revisions: dict[str, tuple[int, int]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    registered_pages: set[int] = field(default_factory=set)
    previous_url: str = ""
    previous_title: str = ""
    previous_content_hash: str = ""


@dataclass(frozen=True, slots=True)
class _ElementReference:
    locator: Locator
    page_revision: int
    dom_revision: int
    url: str


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def _origin(url: str) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    netloc = parts.hostname
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return f"{parts.scheme}://{netloc}"


def _redact_observation(text: str) -> str:
    return _CARD_NUMBER.sub("[PAYMENT FIELD REDACTED]", redact_sensitive_text(text))


class BrowserRuntime:
    """Own persistent Chromium contexts while durable state owns coordination."""

    def __init__(
        self, config: BrowserConfig, state: BrowserState | None = None
    ) -> None:
        if not config.enabled:
            raise BrowserConfigurationError(
                "Browser control is disabled in Ariadne's private configuration."
            )
        self.config = config
        self.state = state or BrowserState(
            config.state.resolve(), lease_seconds=config.lease_seconds
        )
        self._playwright: Playwright | None = None
        self._profiles: dict[str, _OpenProfile] = {}
        self._tasks: dict[str, _TaskRuntime] = {}
        self._closing = False

    async def start(self) -> None:
        self._prepare_private_paths()
        self.state.initialize()
        try:
            self._playwright = await async_playwright().start()
        except PlaywrightError as error:
            raise BrowserConfigurationError(
                "Playwright could not start; install its Chromium runtime first."
            ) from error
        self._prune_retained_artifacts()

    async def close(self) -> None:
        self._closing = True
        for opened in tuple(self._profiles.values()):
            try:
                await opened.context.close()
            except PlaywrightError:
                pass
        self._profiles.clear()
        self._tasks.clear()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    def _prepare_private_paths(self) -> None:
        for path in (
            self.config.state.resolve().parent,
            self.config.profiles.resolve(),
            self.config.artifacts.resolve(),
            self.config.socket.resolve().parent,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        for child in ("screenshots", "downloads"):
            path = self.config.artifacts.resolve() / child
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)

    def _prune_retained_artifacts(self) -> None:
        artifact_root = self.config.artifacts.resolve()
        for path in self.state.prune_journal(
            retention_days=self.config.journal_retention_days,
            maximum_entries=self.config.journal_max_entries,
        ):
            resolved = path.resolve()
            if resolved.is_relative_to(artifact_root) and resolved.is_file():
                resolved.unlink(missing_ok=True)

    async def health(self) -> dict[str, Any]:
        return {
            "status": "ready" if self._playwright is not None else "starting",
            "engine": "chromium",
            "headless": self.config.headless,
            "open_profiles": sorted(self._profiles),
            "active_tasks": sorted(self._tasks),
            "socket": str(self.config.socket.resolve()),
        }

    async def create_profile(self, name: str) -> dict[str, Any]:
        root = self.config.profiles.resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise BrowserStateError("Browser profile path escaped its private root.")
        profile = self.state.create_profile(name, path)
        self.state.record_journal(
            "profile.create", "succeeded", detail=f"profile={name}"
        )
        return {"profile": profile.public_payload()}

    async def list_profiles(self) -> dict[str, Any]:
        profiles = self.state.profiles()
        return {
            "profiles": [profile.public_payload() for profile in profiles],
            "count": len(profiles),
        }

    async def inspect_profile(self, name: str) -> dict[str, Any]:
        profile = self.state.profile(name)
        owner = next(
            (
                task_id
                for task_id, task in self._tasks.items()
                if task.profile_name == name
            ),
            None,
        )
        return {"profile": profile.public_payload(), "active_task": owner}

    async def journal(self, limit: int = 100) -> dict[str, Any]:
        entries = self.state.journal(limit=limit)
        return {
            "entries": [entry.public_payload() for entry in entries],
            "count": len(entries),
        }

    async def list_uncertain(self) -> dict[str, Any]:
        actions = self.state.uncertain_actions()
        return {
            "operations": [
                {
                    "operation_id": action.operation_id,
                    "task_id": action.task_id,
                    "status": action.status.value,
                }
                for action in actions
            ],
            "count": len(actions),
        }

    async def resolve_uncertain(
        self, operation_id: str, outcome: str
    ) -> dict[str, Any]:
        action = self.state.resolve_uncertain(operation_id, outcome=outcome)
        self._tasks.pop(action.task_id, None)
        self.state.record_journal(
            "recovery.resolve",
            outcome,
            task_id=action.task_id,
            detail=f"operation={operation_id}",
        )
        return {
            "operation_id": action.operation_id,
            "task_id": action.task_id,
            "status": action.status.value,
            "profile_released": True,
        }

    async def start_session(
        self,
        profile: str,
        task_id: str,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        if task_id in self._tasks:
            lease = self.state.acquire(profile, task_id, lease_token=lease_token)
            observation = await self._observe(task_id, lease.token)
            current = self.state.require_lease(
                task_id, lease.token, allow_takeover=True
            )
            return {
                "lease": current.public_payload(include_token=True),
                "observation": observation,
            }
        lease = self.state.acquire(profile, task_id, lease_token=lease_token)
        opened = await self._open_profile(profile)
        pages = opened.context.pages
        page = pages[-1] if pages else await opened.context.new_page()
        self._tasks[task_id] = _TaskRuntime(profile, page)
        self._register_page(task_id, page)
        revision = self.state.bump_revision(task_id, lease.token)
        self.state.record_journal(
            "session.start", "succeeded", task_id=task_id, origin=_origin(page.url)
        )
        observation = await self._observe(task_id, lease.token)
        observation["page_revision"] = revision
        current = self.state.require_lease(task_id, lease.token, allow_takeover=True)
        return {
            "lease": current.public_payload(include_token=True),
            "observation": observation,
        }

    async def release_session(self, task_id: str, lease_token: str) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token, allow_takeover=True)
        lease = self.state.release(task_id, lease_token)
        self._tasks.pop(task_id, None)
        self.state.record_journal(
            "session.release",
            "succeeded",
            task_id=task_id,
            origin=_origin(runtime.page.url),
        )
        return {"lease": lease.public_payload()}

    async def _open_profile(self, name: str) -> _OpenProfile:
        current = self._profiles.get(name)
        if current is not None:
            return current
        if self._playwright is None:
            raise BrowserConfigurationError("Browser service is not ready.")
        profile = self.state.profile(name)
        kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "accept_downloads": True,
            "downloads_path": str(self.config.artifacts.resolve() / "downloads"),
            "viewport": {"width": 1440, "height": 1000},
            "args": ["--disable-dev-shm-usage"],
        }
        if self.config.executable_path is not None:
            kwargs["executable_path"] = str(self.config.executable_path.resolve())
        try:
            context = await self._playwright.chromium.launch_persistent_context(
                str(profile.user_data_path), **kwargs
            )
        except PlaywrightError as error:
            raise BrowserConfigurationError(
                "Chromium could not open the private profile. Check display access, "
                "browser dependencies, and whether another process owns it."
            ) from error
        await context.expose_function(
            "__ariadneReportPermission",
            lambda kind: self._permission_event(name, str(kind)),
        )
        await context.add_init_script(
            """(() => {
                const report = kind => {
                    window.__ariadneReportPermission(kind).catch(() => {});
                };
                if (navigator.mediaDevices?.getUserMedia) {
                    const original = navigator.mediaDevices.getUserMedia.bind(
                        navigator.mediaDevices);
                    navigator.mediaDevices.getUserMedia = constraints => {
                        report(`media:${Object.keys(constraints).sort().join(',')}`);
                        return original(constraints);
                    };
                }
                if (window.Notification?.requestPermission) {
                    const original = Notification.requestPermission.bind(Notification);
                    Notification.requestPermission = callback => {
                        report('notifications');
                        return original(callback);
                    };
                }
                for (const method of ['getCurrentPosition', 'watchPosition']) {
                    if (navigator.geolocation?.[method]) {
                        const original = navigator.geolocation[method].bind(
                            navigator.geolocation);
                        navigator.geolocation[method] = (...args) => {
                            report('geolocation');
                            return original(...args);
                        };
                    }
                }
            })()"""
        )
        context.set_default_timeout(self.config.action_timeout_seconds * 1000)
        opened = _OpenProfile(context)
        self._profiles[name] = opened
        return opened

    def _task(
        self, task_id: str, lease_token: str, *, allow_takeover: bool = False
    ) -> _TaskRuntime:
        self.state.require_lease(task_id, lease_token, allow_takeover=allow_takeover)
        runtime = self._tasks.get(task_id)
        if runtime is None:
            raise BrowserLeaseError(
                "Browser task is not attached to this service process; start the "
                "session again with its lease token."
            )
        return runtime

    def _register_page(self, task_id: str, page: Page) -> None:
        runtime = self._tasks.get(task_id)
        if runtime is None or id(page) in runtime.registered_pages:
            return
        runtime.registered_pages.add(id(page))
        page.on("framenavigated", lambda _: self._invalidate_document(task_id))
        page.on(
            "dialog",
            lambda dialog: asyncio.create_task(self._dismiss_dialog(task_id, dialog)),
        )
        page.on(
            "popup",
            lambda popup: self._register_popup(task_id, popup),
        )
        page.on(
            "download",
            lambda download: self._append_event(
                task_id,
                {
                    "type": "download_started",
                    "suggested_filename": download.suggested_filename,
                },
            ),
        )

    def _register_popup(self, task_id: str, popup: Page) -> None:
        self._register_page(task_id, popup)
        self._append_event(task_id, {"type": "popup", "url": _safe_url(popup.url)})

    async def _dismiss_dialog(self, task_id: str, dialog: Dialog) -> None:
        self._append_event(
            task_id,
            {
                "type": "dialog",
                "dialog_type": dialog.type,
                "message": _redact_observation(dialog.message)[:500],
                "disposition": "dismissed",
            },
        )
        try:
            await dialog.dismiss()
        except PlaywrightError:
            pass

    def _append_event(self, task_id: str, event: dict[str, Any]) -> None:
        runtime = self._tasks.get(task_id)
        if runtime is not None:
            runtime.events.append(event)
            del runtime.events[:-20]

    def _invalidate_document(self, task_id: str) -> None:
        runtime = self._tasks.get(task_id)
        if runtime is not None:
            runtime.references.clear()
            runtime.screenshot_revisions.clear()

    def _permission_event(self, profile_name: str, kind: str) -> None:
        for task_id, runtime in self._tasks.items():
            if runtime.profile_name == profile_name:
                self._append_event(
                    task_id,
                    {
                        "type": "browser_permission_requested",
                        "permission": _redact_observation(kind)[:100],
                        "disposition": "not_auto_accepted",
                    },
                )

    async def list_tabs(self, task_id: str, lease_token: str) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token)
        context = self._profiles[runtime.profile_name].context
        tabs = []
        for index, page in enumerate(context.pages):
            tabs.append(
                {
                    "id": f"tab-{index}",
                    "url": _safe_url(page.url),
                    "title": _redact_observation(await page.title())[:300],
                    "current": page is runtime.page,
                }
            )
        return {"tabs": tabs, "count": len(tabs)}

    async def select_tab(
        self, task_id: str, lease_token: str, tab_id: str
    ) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token)
        context = self._profiles[runtime.profile_name].context
        index = self._tab_index(tab_id, len(context.pages))
        runtime.page = context.pages[index]
        self._register_page(task_id, runtime.page)
        await runtime.page.bring_to_front()
        self.state.bump_revision(task_id, lease_token)
        runtime.references.clear()
        return await self._observe(task_id, lease_token)

    async def close_tab(
        self, task_id: str, lease_token: str, tab_id: str
    ) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token)
        context = self._profiles[runtime.profile_name].context
        if len(context.pages) <= 1:
            raise BrowserStateError("Keep at least one tab open in a task session.")
        index = self._tab_index(tab_id, len(context.pages))
        closing = context.pages[index]
        await closing.close()
        if runtime.page is closing:
            runtime.page = context.pages[-1]
            self._register_page(task_id, runtime.page)
        self.state.bump_revision(task_id, lease_token)
        runtime.references.clear()
        return await self.list_tabs(task_id, lease_token)

    @staticmethod
    def _tab_index(tab_id: str, count: int) -> int:
        if not tab_id.startswith("tab-") or not tab_id[4:].isdigit():
            raise BrowserStateError("Tab id must come from browser_list_tabs.")
        index = int(tab_id[4:])
        if index >= count:
            raise BrowserStateError("Tab id is no longer available.")
        return index

    async def navigate(
        self, task_id: str, lease_token: str, url: str
    ) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token)
        parts = urlsplit(url.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise BrowserStateError("Browser navigation accepts absolute HTTP(S) URLs.")
        if parts.username or parts.password:
            raise BrowserStateError("Do not put credentials in a browser URL.")
        before_origin = _origin(runtime.page.url)
        try:
            await runtime.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as error:
            self._journal_failure(task_id, runtime, "navigate", error)
            raise BrowserStateError("Browser navigation did not complete.") from error
        self._after_mutation(task_id, lease_token, runtime)
        observation = await self._observe(task_id, lease_token)
        after_origin = _origin(runtime.page.url)
        if after_origin != before_origin:
            observation["new_origin"] = after_origin
        self._journal_success(task_id, runtime, "navigate")
        return observation

    async def inspect(self, task_id: str, lease_token: str) -> dict[str, Any]:
        self._task(task_id, lease_token)
        return await self._observe(task_id, lease_token)

    async def click(
        self,
        task_id: str,
        lease_token: str,
        ref: str,
        *,
        action_kind: ActionKind = "reversible",
        operation_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token)
        if action_kind not in {"reversible", "consequential"}:
            raise BrowserStateError("Browser action kind is invalid.")
        operation_started = False
        if action_kind == "consequential":
            if operation_id is None or approval_id is None:
                raise BrowserApprovalError(
                    "Consequential clicks require an operation id and trusted approval."
                )
            previous = self.state.action(operation_id)
            if previous is not None:
                if previous.status == ActionOutcome.SUCCEEDED:
                    assert previous.result is not None
                    return previous.result
                raise BrowserUncertainError(
                    "This consequential operation was already attempted. Inspect the "
                    "site's definitive status instead of trying it again."
                )
        locator = await self._reference(runtime, ref)
        before_origin = _origin(runtime.page.url)
        href = await locator.get_attribute("href")
        if href:
            scheme = urlsplit(href).scheme.lower()
            if scheme and scheme not in {"http", "https", "javascript"}:
                self._append_event(
                    task_id,
                    {"type": "external_application_requested", "scheme": scheme},
                )
                raise BrowserStateError(
                    "The element requested an external application. It was surfaced "
                    "without being launched."
                )
        if action_kind == "consequential":
            assert operation_id is not None
            assert approval_id is not None
            current = await self._material_state(runtime)
            action = self.state.begin_consequential(
                operation_id=operation_id,
                task_id=task_id,
                lease_token=lease_token,
                approval_id=approval_id,
                digest=state_digest(current),
            )
            assert action.status == ActionOutcome.PENDING
            operation_started = True
        try:
            await locator.click()
            await runtime.page.wait_for_timeout(100)
            self._after_mutation(task_id, lease_token, runtime)
            result = await self._observe(task_id, lease_token)
            after_origin = _origin(runtime.page.url)
            if after_origin != before_origin:
                result["new_origin"] = after_origin
            if operation_started and operation_id is not None:
                self.state.finish_consequential(
                    operation_id, succeeded=True, result=result
                )
        except Exception as error:
            if operation_started and operation_id is not None:
                with suppress(BrowserStateError):
                    self.state.mark_uncertain(operation_id)
                self._journal_failure(task_id, runtime, "click.consequential", error)
                raise BrowserStateError(
                    "The consequential click outcome is uncertain. Do not click again; "
                    "inspect the site's definitive status."
                ) from error
            if isinstance(error, PlaywrightError):
                self._journal_failure(task_id, runtime, "click", error)
                raise BrowserStateError(
                    "The referenced element could not be clicked."
                ) from error
            raise
        self._journal_success(
            task_id,
            runtime,
            "click.consequential" if operation_started else "click",
        )
        return result

    async def type_text(
        self,
        task_id: str,
        lease_token: str,
        ref: str,
        text: str,
        *,
        clear: bool = True,
    ) -> dict[str, Any]:
        if not text or len(text) > 20_000:
            raise BrowserStateError("Typed text must contain 1-20,000 characters.")
        runtime = self._task(task_id, lease_token)
        locator = await self._reference(runtime, ref)
        metadata = cast(
            dict[str, Any],
            await locator.evaluate(
                """element => ({
                    type: element.getAttribute('type') || '',
                    name: element.getAttribute('name') || '',
                    autocomplete: element.getAttribute('autocomplete') || '',
                    aria: element.getAttribute('aria-label') || ''
                })"""
            ),
        )
        descriptor = " ".join(str(value) for value in metadata.values())
        if metadata.get("type") == "password" or _SENSITIVE_FIELD.search(descriptor):
            raise BrowserStateError(
                "Password, MFA, and payment fields require private human takeover; "
                "their values are never accepted through the model tool."
            )
        try:
            if clear:
                await locator.fill(text)
            else:
                await locator.press_sequentially(text)
        except PlaywrightError as error:
            self._journal_failure(task_id, runtime, "type", error)
            raise BrowserStateError(
                "Text could not be entered in that field."
            ) from error
        self._after_mutation(task_id, lease_token, runtime)
        self._journal_success(task_id, runtime, "type", detail="field value omitted")
        return await self._observe(task_id, lease_token)

    async def select(
        self,
        task_id: str,
        lease_token: str,
        ref: str,
        values: list[str],
    ) -> dict[str, Any]:
        if not values or len(values) > 20 or any(len(value) > 500 for value in values):
            raise BrowserStateError("Select between 1 and 20 bounded option values.")
        runtime = self._task(task_id, lease_token)
        try:
            await (await self._reference(runtime, ref)).select_option(values)
        except PlaywrightError as error:
            self._journal_failure(task_id, runtime, "select", error)
            raise BrowserStateError(
                "The requested option could not be selected."
            ) from error
        self._after_mutation(task_id, lease_token, runtime)
        self._journal_success(task_id, runtime, "select")
        return await self._observe(task_id, lease_token)

    async def scroll(
        self,
        task_id: str,
        lease_token: str,
        direction: Literal["up", "down"],
        amount: int = 700,
    ) -> dict[str, Any]:
        if not 100 <= amount <= 5_000:
            raise BrowserStateError(
                "Scroll amount must be between 100 and 5,000 pixels."
            )
        runtime = self._task(task_id, lease_token)
        delta = amount if direction == "down" else -amount
        await runtime.page.mouse.wheel(0, delta)
        self._after_mutation(task_id, lease_token, runtime)
        self._journal_success(task_id, runtime, "scroll")
        return await self._observe(task_id, lease_token)

    async def upload(
        self,
        task_id: str,
        lease_token: str,
        ref: str,
        path: str,
    ) -> dict[str, Any]:
        artifact = Path(path).expanduser().resolve()
        if not artifact.is_file():
            raise BrowserStateError("Upload path must name an existing local file.")
        runtime = self._task(task_id, lease_token)
        try:
            await (await self._reference(runtime, ref)).set_input_files(str(artifact))
        except PlaywrightError as error:
            self._journal_failure(task_id, runtime, "upload", error)
            raise BrowserStateError(
                "The selected file could not be attached."
            ) from error
        self._after_mutation(task_id, lease_token, runtime)
        self._journal_success(
            task_id, runtime, "upload", detail=f"artifact={artifact.name}"
        )
        return await self._observe(task_id, lease_token)

    async def download(
        self, task_id: str, lease_token: str, ref: str
    ) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token)
        locator = await self._reference(runtime, ref)
        try:
            async with runtime.page.expect_download() as pending:
                await locator.click()
            download = await pending.value
            retained = await self._retain_download(task_id, download)
        except PlaywrightError as error:
            self._journal_failure(task_id, runtime, "download", error)
            raise BrowserStateError(
                "The expected browser download did not complete."
            ) from error
        self._after_mutation(task_id, lease_token, runtime)
        self.state.record_journal(
            "download",
            "succeeded",
            task_id=task_id,
            origin=_origin(runtime.page.url),
            detail=f"filename={retained.name}",
            artifact_path=retained,
        )
        observation = await self._observe(task_id, lease_token)
        observation["download"] = {
            "path": str(retained),
            "filename": retained.name,
            "status": "inert_private_artifact",
        }
        return observation

    async def _retain_download(self, task_id: str, download: Download) -> Path:
        filename = _SAFE_FILENAME.sub("_", download.suggested_filename).strip(". ")
        if not filename:
            filename = "download"
        directory = self.config.artifacts.resolve() / "downloads" / task_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        destination = directory / f"{secrets.token_hex(6)}-{filename[:160]}"
        await download.save_as(str(destination))
        destination.chmod(0o600)
        return destination

    async def wait(
        self,
        task_id: str,
        lease_token: str,
        *,
        text: str | None = None,
        url: str | None = None,
        seconds: float | None = None,
    ) -> dict[str, Any]:
        selected = sum(value is not None for value in (text, url, seconds))
        if selected != 1:
            raise BrowserStateError("Wait for exactly one of text, URL, or seconds.")
        runtime = self._task(task_id, lease_token)
        timeout = self.config.action_timeout_seconds * 1000
        try:
            if text is not None:
                if not text.strip() or len(text) > 500:
                    raise BrowserStateError("Wait text must be 1-500 characters.")
                await runtime.page.get_by_text(text, exact=False).first.wait_for(
                    state="visible", timeout=timeout
                )
            elif url is not None:
                if len(url) > 2_000:
                    raise BrowserStateError("Wait URL pattern is too long.")
                await runtime.page.wait_for_url(url, timeout=timeout)
            else:
                assert seconds is not None
                if not 0 <= seconds <= min(30, self.config.action_timeout_seconds):
                    raise BrowserStateError(
                        "Wait seconds exceeds the configured bound."
                    )
                await runtime.page.wait_for_timeout(seconds * 1000)
        except PlaywrightError as error:
            raise BrowserStateError("The bounded browser wait timed out.") from error
        self._after_mutation(task_id, lease_token, runtime)
        return await self._observe(task_id, lease_token)

    async def screenshot(
        self, task_id: str, lease_token: str, *, full_page: bool = False
    ) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token)
        lease = self.state.require_lease(task_id, lease_token)
        screenshot_id = f"shot-{secrets.token_hex(8)}"
        directory = self.config.artifacts.resolve() / "screenshots" / task_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / f"{screenshot_id}.png"
        try:
            await runtime.page.screenshot(path=str(path), full_page=full_page)
        except PlaywrightError as error:
            raise BrowserStateError(
                "Browser screenshot could not be captured."
            ) from error
        path.chmod(0o600)
        runtime.screenshot_revisions[screenshot_id] = (
            lease.page_revision,
            await self._dom_revision(runtime.page),
        )
        self.state.record_journal(
            "screenshot",
            "succeeded",
            task_id=task_id,
            origin=_origin(runtime.page.url),
            artifact_path=path,
        )
        return {
            "screenshot_id": screenshot_id,
            "path": str(path),
            "page_revision": lease.page_revision,
            "full_page": full_page,
        }

    async def coordinate_click(
        self,
        task_id: str,
        lease_token: str,
        screenshot_id: str,
        x: float,
        y: float,
    ) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token)
        lease = self.state.require_lease(task_id, lease_token)
        current_evidence = (lease.page_revision, await self._dom_revision(runtime.page))
        if runtime.screenshot_revisions.get(screenshot_id) != current_evidence:
            raise BrowserReferenceError(
                "Coordinate interaction requires a screenshot of the current page."
            )
        viewport = runtime.page.viewport_size
        if viewport is None or not (
            0 <= x <= viewport["width"] and 0 <= y <= viewport["height"]
        ):
            raise BrowserStateError(
                "Click coordinates are outside the current viewport."
            )
        try:
            await runtime.page.mouse.click(x, y)
        except PlaywrightError as error:
            raise BrowserStateError(
                "Coordinate click could not be completed."
            ) from error
        self._after_mutation(task_id, lease_token, runtime)
        self._journal_success(task_id, runtime, "coordinate_click")
        return await self._observe(task_id, lease_token)

    async def request_takeover(
        self, task_id: str, lease_token: str, reason: str
    ) -> dict[str, Any]:
        if not reason.strip() or len(reason) > 500:
            raise BrowserStateError("Takeover reason must be 1-500 characters.")
        runtime = self._task(task_id, lease_token)
        lease = self.state.request_takeover(task_id, lease_token)
        self.state.record_journal(
            "takeover.request",
            "waiting_for_human",
            task_id=task_id,
            origin=_origin(runtime.page.url),
            detail=reason,
        )
        return {
            "lease": lease.public_payload(),
            "takeover_url": (
                str(self.config.takeover_url) if self.config.takeover_url else None
            ),
            "instruction": (
                "Use the private browser view, finish only the human-required step, "
                "then tell Iris to resume. Takeover is not approval for a later "
                "consequential action."
            ),
        }

    async def resume_takeover(self, task_id: str, lease_token: str) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token, allow_takeover=True)
        lease = self.state.resume_takeover(task_id, lease_token)
        runtime.references.clear()
        self.state.record_journal(
            "takeover.resume",
            "succeeded",
            task_id=task_id,
            origin=_origin(runtime.page.url),
        )
        return {
            "lease": lease.public_payload(),
            "observation": await self._observe(task_id, lease_token),
        }

    async def record_confirmation(
        self,
        *,
        approval_id: str,
        task_id: str,
        lease_token: str,
        page_revision: int,
        digest: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        """Trusted integration boundary; deliberately absent from model MCP tools."""
        runtime = self._task(task_id, lease_token)
        material = await self._material_state(runtime)
        actual = state_digest(material)
        if not secrets.compare_digest(actual, digest):
            raise BrowserApprovalError(
                "Material browser state changed before confirmation was recorded."
            )
        self.state.record_confirmation(
            approval_id=approval_id,
            task_id=task_id,
            lease_token=lease_token,
            page_revision=page_revision,
            digest=digest,
            ttl_seconds=ttl_seconds,
        )
        return {"approval_id": approval_id, "status": "recorded"}

    async def _reference(self, runtime: _TaskRuntime, ref: str) -> Locator:
        reference = runtime.references.get(ref)
        if reference is None:
            raise BrowserReferenceError(
                "Element reference is stale or unknown; inspect the page again."
            )
        if (
            reference.url != runtime.page.url
            or reference.dom_revision != await self._dom_revision(runtime.page)
        ):
            runtime.references.clear()
            runtime.screenshot_revisions.clear()
            raise BrowserReferenceError(
                "The page changed after inspection; inspect it again for fresh "
                "element references."
            )
        return reference.locator

    def _after_mutation(
        self, task_id: str, lease_token: str, runtime: _TaskRuntime
    ) -> None:
        self.state.bump_revision(task_id, lease_token)
        runtime.references.clear()
        runtime.screenshot_revisions.clear()

    async def _observe(self, task_id: str, lease_token: str) -> dict[str, Any]:
        runtime = self._task(task_id, lease_token)
        page = runtime.page
        lease = self.state.require_lease(task_id, lease_token)
        title = _redact_observation(await page.title())[:300]
        url = _safe_url(page.url)
        try:
            body = await page.locator("body").inner_text(timeout=3_000)
        except PlaywrightError:
            body = ""
        body = _redact_observation(body)
        maximum = self.config.observation_max_chars
        truncated = len(body) > maximum
        body = body[:maximum]
        content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        changes = {
            "url": bool(runtime.previous_url and runtime.previous_url != url),
            "title": bool(runtime.previous_title and runtime.previous_title != title),
            "content": bool(
                runtime.previous_content_hash
                and runtime.previous_content_hash != content_hash
            ),
        }
        runtime.previous_url = url
        runtime.previous_title = title
        runtime.previous_content_hash = content_hash
        elements = await self._elements(runtime, lease.page_revision)
        events = runtime.events[-20:]
        runtime.events.clear()
        material = await self._material_state(runtime, body=body)
        payload: dict[str, Any] = {
            "task_id": task_id,
            "url": url,
            "origin": _origin(url),
            "title": title,
            "page_revision": lease.page_revision,
            "state_digest": state_digest(material),
            "text": body,
            "text_truncated": truncated,
            "elements": elements,
            "changes": changes,
            "events": events,
        }
        if any(bool(element["sensitive"]) for element in elements):
            takeover = self.state.request_takeover(task_id, lease_token)
            payload["task_state"] = takeover.state.value
            payload["human_attention_required"] = {
                "reason": "login_mfa_or_payment_field",
                "takeover_url": (
                    str(self.config.takeover_url) if self.config.takeover_url else None
                ),
            }
            self.state.record_journal(
                "sensitive_field.detected",
                "waiting_for_human",
                task_id=task_id,
                origin=_origin(runtime.page.url),
            )
        else:
            payload["task_state"] = lease.state.value
        return payload

    async def _elements(
        self, runtime: _TaskRuntime, page_revision: int
    ) -> list[dict[str, Any]]:
        runtime.references.clear()
        dom_revision = await self._dom_revision(runtime.page)
        candidates = runtime.page.locator(_INTERACTIVE_SELECTOR)
        count = min(await candidates.count(), self.config.observation_max_elements)
        elements: list[dict[str, Any]] = []
        for index in range(count):
            locator = candidates.nth(index)
            try:
                if not await locator.is_visible():
                    continue
                metadata = cast(
                    dict[str, Any],
                    await locator.evaluate(
                        """element => {
                            const tag = element.tagName.toLowerCase();
                            const type = element.getAttribute('type') || '';
                            const explicitRole = element.getAttribute('role');
                            const inferred = tag === 'a' ? 'link' :
                                tag === 'button' ? 'button' :
                                tag === 'select' ? 'combobox' :
                                tag === 'textarea' ? 'textbox' :
                                tag === 'input' &&
                                    ['button','submit','reset'].includes(type)
                                    ? 'button' :
                                    tag === 'input' ? 'textbox' : tag;
                            const labels = element.labels
                                ? Array.from(element.labels)
                                    .map(label => Array.from(label.childNodes)
                                        .filter(node =>
                                            node.nodeType === Node.TEXT_NODE)
                                        .map(node => node.textContent).join(' '))
                                    .join(' ')
                                : '';
                            return {
                                tag,
                                type,
                                role: explicitRole || inferred,
                                name: element.getAttribute('aria-label') || labels ||
                                    element.getAttribute('title') ||
                                    element.getAttribute('placeholder') ||
                                    element.innerText ||
                                    element.getAttribute('alt') || '',
                                autocomplete: element.getAttribute('autocomplete') || ''
                            };
                        }"""
                    ),
                )
                descriptor = " ".join(str(value) for value in metadata.values())
                sensitive = metadata.get("type") == "password" or (
                    metadata.get("tag") in {"input", "textarea"}
                    and bool(_SENSITIVE_FIELD.search(descriptor))
                )
                ref = f"r{page_revision}.{dom_revision}-{len(elements) + 1}"
                runtime.references[ref] = _ElementReference(
                    locator=locator,
                    page_revision=page_revision,
                    dom_revision=dom_revision,
                    url=runtime.page.url,
                )
                elements.append(
                    {
                        "ref": ref,
                        "role": str(metadata.get("role", "element"))[:80],
                        "name": " ".join(
                            _redact_observation(str(metadata.get("name", ""))).split()
                        )[:300],
                        "enabled": await locator.is_enabled(),
                        "sensitive": sensitive,
                    }
                )
            except PlaywrightError:
                continue
        return elements

    @staticmethod
    async def _dom_revision(page: Page) -> int:
        value = await page.evaluate(
            """() => {
                if (window.__ariadneDomRevision === undefined) {
                    window.__ariadneDomRevision = 0;
                    new MutationObserver(() => window.__ariadneDomRevision++)
                        .observe(document.documentElement, {
                            subtree: true,
                            childList: true,
                            attributes: true,
                            characterData: true
                        });
                }
                return window.__ariadneDomRevision;
            }"""
        )
        return int(value)

    async def _material_state(
        self, runtime: _TaskRuntime, *, body: str | None = None
    ) -> dict[str, Any]:
        if body is None:
            try:
                body = await runtime.page.locator("body").inner_text(timeout=3_000)
            except PlaywrightError:
                body = ""
            body = _redact_observation(body)[: self.config.observation_max_chars]
        form_state = cast(
            list[dict[str, Any]],
            await runtime.page.locator("input, select, textarea").evaluate_all(
                """elements => elements.slice(0, 500).map((element, index) => {
                    const type = (element.getAttribute('type') || '').toLowerCase();
                    const autocomplete = (
                        element.getAttribute('autocomplete') || '').toLowerCase();
                    const descriptor = [
                        element.getAttribute('name') || '',
                        element.getAttribute('id') || '',
                        element.getAttribute('aria-label') || ''
                    ].join(' ').toLowerCase();
                    const sensitive = type === 'password' ||
                        autocomplete.startsWith('cc-') ||
                        autocomplete === 'one-time-code' ||
                        /(password|passcode|otp|cvv|cvc|security.?code)/.test(
                            descriptor);
                    return {
                        index,
                        type,
                        checked: Boolean(element.checked),
                        value: sensitive
                            ? (element.value ? '<present>' : '<empty>')
                            : String(element.value || '').slice(0, 1000)
                    };
                })"""
            ),
        )
        form_hash = hashlib.sha256(
            json.dumps(
                form_state,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "url": _safe_url(runtime.page.url),
            "title": _redact_observation(await runtime.page.title())[:300],
            "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "form_hash": form_hash,
        }

    def _journal_success(
        self,
        task_id: str,
        runtime: _TaskRuntime,
        operation: str,
        *,
        detail: str | None = None,
    ) -> None:
        self.state.record_journal(
            operation,
            "succeeded",
            task_id=task_id,
            origin=_origin(runtime.page.url),
            detail=detail,
        )

    def _journal_failure(
        self,
        task_id: str,
        runtime: _TaskRuntime,
        operation: str,
        error: Exception,
    ) -> None:
        self.state.record_journal(
            operation,
            "failed",
            task_id=task_id,
            origin=_origin(runtime.page.url),
            detail=type(error).__name__,
        )


async def with_runtime(
    config: BrowserConfig,
    operation: Callable[[BrowserRuntime], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Small helper for one-shot diagnostics and tests."""
    runtime = BrowserRuntime(config)
    await runtime.start()
    try:
        return await operation(runtime)
    finally:
        await runtime.close()


__all__ = ["ActionKind", "BrowserRuntime", "with_runtime"]
