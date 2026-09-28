import pytest

from fwasset.core.services.music_flash_service import run_music_flash


def test_run_music_flash_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fwasset.core.services.music_flash_service.format_usb",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "fwasset.core.services.music_flash_service.copy_directory_to_usb",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "fwasset.core.services.music_flash_service.eject_usb",
        lambda *args, **kwargs: True,
    )

    result = run_music_flash("D:/music", "E:/", log_fn=lambda _m: None)

    assert result["ok"] is True
    assert result["code"] == "ok"
    assert result["payload"]["formatted"] is True
    assert result["payload"]["copied"] is True
    assert result["payload"]["ejected"] is True
    assert result["payload"]["eject_requested"] is True


def test_run_music_flash_format_failed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fwasset.core.services.music_flash_service.format_usb",
        lambda *args, **kwargs: False,
    )

    result = run_music_flash("D:/music", "E:/", log_fn=lambda _m: None)

    assert result["ok"] is False
    assert result["code"] == "format_failed"


def test_run_music_flash_copy_failed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fwasset.core.services.music_flash_service.format_usb",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "fwasset.core.services.music_flash_service.copy_directory_to_usb",
        lambda *args, **kwargs: False,
    )

    result = run_music_flash("D:/music", "E:/", log_fn=lambda _m: None)

    assert result["ok"] is False
    assert result["code"] == "copy_failed"


def test_run_music_flash_without_format_and_eject(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fwasset.core.services.music_flash_service.copy_directory_to_usb",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "fwasset.core.services.music_flash_service.eject_usb",
        lambda *args, **kwargs: True,
    )

    result = run_music_flash(
        "D:/music", "E:/", format_first=False, eject_after=False, log_fn=lambda _m: None
    )

    assert result["ok"] is True
    assert result["payload"]["formatted"] is False
    assert result["payload"]["ejected"] is False
