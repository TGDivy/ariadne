from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ariadne.browser import (
    ActionOutcome,
    BrowserApprovalError,
    BrowserBusyError,
    BrowserLeaseError,
    BrowserState,
    BrowserStateError,
    BrowserUncertainError,
    TaskState,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 11, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **values: float) -> None:
        self.value += timedelta(**values)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def state(tmp_path: Path, clock: Clock) -> BrowserState:
    browser = BrowserState(
        tmp_path / "private" / "browser.sqlite3",
        lease_seconds=60,
        clock=clock,
    )
    browser.initialize()
    browser.create_profile("personal", tmp_path / "profiles" / "personal")
    return browser


def test_private_state_and_profile_paths_have_restrictive_modes(
    state: BrowserState, tmp_path: Path
) -> None:
    assert state.path.stat().st_mode & 0o777 == 0o600
    assert state.path.parent.stat().st_mode & 0o777 == 0o700
    profile = state.profile("personal")
    assert profile.user_data_path == (tmp_path / "profiles" / "personal").resolve()
    assert profile.user_data_path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("name", ["", "Personal", "../escape", "a/b", "-bad"])
def test_profile_names_cannot_escape_the_private_root(
    state: BrowserState, tmp_path: Path, name: str
) -> None:
    with pytest.raises(BrowserStateError):
        state.create_profile(name, tmp_path / "profiles" / name)


def test_only_one_task_can_own_a_profile_and_reattach_requires_its_token(
    state: BrowserState,
) -> None:
    first = state.acquire("personal", "task-one")

    with pytest.raises(BrowserBusyError, match="busy"):
        state.acquire("personal", "task-two")
    with pytest.raises(BrowserLeaseError, match="lease token"):
        state.acquire("personal", "task-one")

    attached = state.acquire("personal", "task-one", lease_token=first.token)
    assert attached.task_id == first.task_id
    assert attached.token == first.token


def test_an_expired_reversible_owner_is_recovered_without_deleting_profile(
    state: BrowserState, clock: Clock
) -> None:
    old = state.acquire("personal", "old-task")
    clock.advance(seconds=61)

    current = state.acquire("personal", "new-task")

    assert current.state == TaskState.ACTIVE
    with pytest.raises(BrowserLeaseError, match="released"):
        state.require_lease(old.task_id, old.token)
    assert state.profile("personal").user_data_path.is_dir()


def test_takeover_blocks_mutation_and_resume_invalidates_page_references(
    state: BrowserState,
) -> None:
    lease = state.acquire("personal", "takeover-task")
    state.bump_revision(lease.task_id, lease.token)
    takeover = state.request_takeover(lease.task_id, lease.token)

    assert takeover.state == TaskState.TAKEOVER
    with pytest.raises(BrowserLeaseError, match="human control"):
        state.require_lease(lease.task_id, lease.token)

    resumed = state.resume_takeover(lease.task_id, lease.token)
    assert resumed.state == TaskState.ACTIVE
    assert resumed.page_revision == 2


def test_release_makes_the_profile_available_without_destroying_it(
    state: BrowserState,
) -> None:
    lease = state.acquire("personal", "task-one")

    released = state.release(lease.task_id, lease.token)
    following = state.acquire("personal", "task-two")

    assert released.state == TaskState.RELEASED
    assert following.state == TaskState.ACTIVE
    assert state.profile("personal").user_data_path.exists()


def test_confirmation_is_bound_to_task_revision_and_material_state(
    state: BrowserState,
) -> None:
    lease = state.acquire("personal", "checkout-task")
    revision = state.bump_revision(lease.task_id, lease.token)
    state.record_confirmation(
        approval_id="approval-1",
        task_id=lease.task_id,
        lease_token=lease.token,
        page_revision=revision,
        digest="basket-a",
        ttl_seconds=60,
    )

    with pytest.raises(BrowserApprovalError, match="state changed"):
        state.begin_consequential(
            operation_id="submit-1",
            task_id=lease.task_id,
            lease_token=lease.token,
            approval_id="approval-1",
            digest="basket-b",
        )

    state.bump_revision(lease.task_id, lease.token)
    with pytest.raises(BrowserApprovalError, match="state changed"):
        state.begin_consequential(
            operation_id="submit-2",
            task_id=lease.task_id,
            lease_token=lease.token,
            approval_id="approval-1",
            digest="basket-a",
        )


def test_confirmation_expires_and_cannot_be_reused(
    state: BrowserState, clock: Clock
) -> None:
    lease = state.acquire("personal", "checkout-task")
    state.record_confirmation(
        approval_id="short-approval",
        task_id=lease.task_id,
        lease_token=lease.token,
        page_revision=0,
        digest="basket",
        ttl_seconds=30,
    )
    clock.advance(seconds=31)

    with pytest.raises(BrowserApprovalError, match="expired"):
        state.begin_consequential(
            operation_id="submit-expired",
            task_id=lease.task_id,
            lease_token=lease.token,
            approval_id="short-approval",
            digest="basket",
        )

    current = state.acquire("personal", lease.task_id, lease_token=lease.token)
    state.record_confirmation(
        approval_id="fresh-approval",
        task_id=current.task_id,
        lease_token=current.token,
        page_revision=current.page_revision,
        digest="basket",
        ttl_seconds=30,
    )
    action = state.begin_consequential(
        operation_id="submit-once",
        task_id=current.task_id,
        lease_token=current.token,
        approval_id="fresh-approval",
        digest="basket",
    )
    assert action.status == ActionOutcome.PENDING
    with pytest.raises(BrowserApprovalError, match="already consumed"):
        state.begin_consequential(
            operation_id="submit-again",
            task_id=current.task_id,
            lease_token=current.token,
            approval_id="fresh-approval",
            digest="basket",
        )


def test_successful_consequential_operation_replays_its_result_exactly_once(
    state: BrowserState,
) -> None:
    lease = state.acquire("personal", "checkout-task")
    state.record_confirmation(
        approval_id="approval",
        task_id=lease.task_id,
        lease_token=lease.token,
        page_revision=0,
        digest="basket",
        ttl_seconds=60,
    )
    state.begin_consequential(
        operation_id="submit-order",
        task_id=lease.task_id,
        lease_token=lease.token,
        approval_id="approval",
        digest="basket",
    )
    state.finish_consequential(
        "submit-order", succeeded=True, result={"order": "fixture-123"}
    )

    replay = state.begin_consequential(
        operation_id="submit-order",
        task_id=lease.task_id,
        lease_token=lease.token,
        approval_id="approval",
        digest="basket",
    )

    assert replay.status == ActionOutcome.SUCCEEDED
    assert replay.result == {"order": "fixture-123"}


def test_lost_consequential_response_blocks_stale_recovery_and_retry(
    state: BrowserState, clock: Clock
) -> None:
    lease = state.acquire("personal", "checkout-task")
    state.record_confirmation(
        approval_id="approval",
        task_id=lease.task_id,
        lease_token=lease.token,
        page_revision=0,
        digest="basket",
        ttl_seconds=60,
    )
    state.begin_consequential(
        operation_id="submit-order",
        task_id=lease.task_id,
        lease_token=lease.token,
        approval_id="approval",
        digest="basket",
    )
    state.mark_uncertain("submit-order")
    clock.advance(minutes=5)

    with pytest.raises(BrowserUncertainError, match="may already"):
        state.require_lease(lease.task_id, lease.token)
    with pytest.raises(BrowserBusyError):
        state.acquire("personal", "different-task")
    with pytest.raises(BrowserUncertainError, match="already attempted"):
        state.begin_consequential(
            operation_id="submit-order",
            task_id=lease.task_id,
            lease_token=lease.token,
            approval_id="approval",
            digest="basket",
        )

    assert [action.operation_id for action in state.uncertain_actions()] == [
        "submit-order"
    ]
    resolved = state.resolve_uncertain("submit-order", outcome="confirmed_succeeded")
    following = state.acquire("personal", "different-task")
    assert resolved.status == ActionOutcome.SUCCEEDED
    assert following.state == TaskState.ACTIVE


def test_journal_is_bounded_and_redacts_secrets(
    state: BrowserState, clock: Clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMPLE_TOKEN", "very-private-token")
    old_artifact = tmp_path / "old.png"
    state.record_journal(
        "navigate",
        "failed",
        origin="https://fixture.invalid",
        detail="token=very-private-token",
        artifact_path=old_artifact,
    )
    clock.advance(days=2)
    state.record_journal("inspect", "succeeded")

    entries = state.journal()
    removed = state.prune_journal(retention_days=1, maximum_entries=100)

    assert "very-private-token" not in (entries[-1].detail or "")
    assert "[REDACTED]" in (entries[-1].detail or "")
    assert removed == (old_artifact,)
    assert [entry.operation for entry in state.journal()] == ["inspect"]
