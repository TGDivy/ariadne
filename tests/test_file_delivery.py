from pathlib import Path

import pytest

from ariadne.telegram import file_delivery
from ariadne.telegram.file_delivery import FileDelivery, FileDeliveryError, StagedFile


def test_stage_records_readable_files_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    document = home / "cv.pdf"
    document.write_bytes(b"cv")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    delivery = FileDelivery(tmp_path / "pending")
    approval_id, files = delivery.stage([str(document)])

    assert approval_id
    assert files[0].path == document
    assert files[0].size_bytes == 2


def test_stage_rejects_files_outside_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"private")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    with pytest.raises(FileDeliveryError, match="outside the allowed home"):
        FileDelivery(tmp_path / "pending").stage([str(outside)])


async def test_request_approval_sends_approve_and_reject_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, object]] = []

    class FakeBot:
        def __init__(self, token: str) -> None:
            assert token == "test-token"

        async def __aenter__(self) -> "FakeBot":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def send_message(self, **kwargs: object) -> None:
            sent.append(kwargs)

    monkeypatch.setattr(file_delivery, "Bot", FakeBot)
    file = tmp_path / "cv.pdf"
    file.write_bytes(b"cv")

    await FileDelivery(tmp_path / "pending").request_approval(
        "approval-id",
        (StagedFile(path=file, size_bytes=2),),
        token="test-token",
        chat_id=7,
    )

    assert sent[0]["chat_id"] == 7
    assert "cv.pdf" in str(sent[0]["text"])
    keyboard = sent[0]["reply_markup"]
    assert (
        keyboard.inline_keyboard[0][0].callback_data
        == "file-delivery:approve:approval-id"
    )
    assert (
        keyboard.inline_keyboard[0][1].callback_data
        == "file-delivery:reject:approval-id"
    )
