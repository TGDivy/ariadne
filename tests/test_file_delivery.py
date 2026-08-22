from pathlib import Path

import pytest

from ariadne.file_delivery import FileDelivery, FileDeliveryError


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
