from pathlib import Path

import pytest

from fwasset.core.services.flash_service import run_handcontrol_copy, run_one_click


def test_run_one_click_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.format_usb", lambda d, log_fn: True
    )
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.copy_to_usb", lambda r, p, d, log_fn: True
    )
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.eject_usb", lambda d, log_fn: True
    )

    result = run_one_click(
        "E:/", "L36", "V1.0.0", "r.ROM", "p.PKG", log_fn=lambda _m: None
    )

    assert result["ok"] is True
    assert result["payload"] == {
        "format_ok": True,
        "copy_ok": True,
        "ejected": True,
    }


def test_run_one_click_format_failed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.format_usb", lambda d, log_fn: False
    )

    result = run_one_click(
        "E:/", "L36", "V1.0.0", "r.ROM", "p.PKG", log_fn=lambda _m: None
    )

    assert result["ok"] is False
    assert result["code"] == "format_failed"
    assert result["payload"]["format_ok"] is False
    assert result["payload"]["copy_ok"] is False


def test_run_one_click_copy_failed(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.format_usb", lambda d, log_fn: True
    )
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.copy_to_usb", lambda r, p, d, log_fn: False
    )

    result = run_one_click(
        "E:/", "L36", "V1.0.0", "r.ROM", "p.PKG", log_fn=lambda _m: None
    )

    assert result["ok"] is False
    assert result["code"] == "copy_failed"
    assert result["payload"]["format_ok"] is True
    assert result["payload"]["copy_ok"] is False


def test_run_one_click_exception(monkeypatch: pytest.MonkeyPatch):
    def raise_format(d, log_fn):
        raise RuntimeError("format boom")

    monkeypatch.setattr("fwasset.core.services.flash_service.format_usb", raise_format)

    result = run_one_click(
        "E:/", "L36", "V1.0.0", "r.ROM", "p.PKG", log_fn=lambda _m: None
    )

    assert result["ok"] is False
    assert result["code"] == "service_exception"


def _program(tmp_path: Path, names: dict[str, str]) -> tuple[Path, Path]:
    program = tmp_path / "YJ_d12x"
    program.mkdir()
    for name, text in names.items():
        (program / name).write_text(text, encoding="utf-8")
    drive = tmp_path / "usb"
    drive.mkdir()
    return program, drive


def test_handcontrol_copy_rom_pkg_formats_then_copies_pair_then_ejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program, drive = _program(
        tmp_path,
        {"a.rom": "rom", "a.pkg": "pkg", "程序信息.toml": "vendor = 'x'\n", "note.pdf": "x"},
    )
    calls: list[str] = []

    def fake_format(target: str, log_fn) -> bool:
        calls.append("format")
        assert target == str(drive)
        return True

    def fake_eject(target: str, log_fn) -> bool:
        calls.append("eject")
        return True

    monkeypatch.setattr("fwasset.core.services.flash_service.format_usb", fake_format)
    monkeypatch.setattr("fwasset.core.services.flash_service.eject_usb", fake_eject)

    result = run_handcontrol_copy(str(drive), str(program), log_fn=lambda _m: None)

    assert calls == ["format", "eject"]
    assert (drive / "a.rom").read_text(encoding="utf-8") == "rom"
    assert (drive / "a.pkg").read_text(encoding="utf-8") == "pkg"
    assert not (drive / "程序信息.toml").exists()
    assert not (drive / "note.pdf").exists()
    assert not (drive / program.name).exists()
    assert result["ok"] is True
    assert result["payload"] == {
        "format_ok": True,
        "copy_ok": True,
        "ejected": True,
    }


def test_handcontrol_copy_img_txt_uses_the_same_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program, drive = _program(
        tmp_path,
        {
            "bootcfg.txt": "boot",
            "d12x.img": "img",
            "程序信息.toml": "vendor = 'x'\n",
        },
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.format_usb",
        lambda _d, _log: calls.append("format") or True,
    )
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.eject_usb",
        lambda _d, _log: calls.append("eject") or True,
    )

    result = run_handcontrol_copy(str(drive), str(program), log_fn=lambda _m: None)

    assert result["ok"] is True
    assert calls == ["format", "eject"]
    assert (drive / "bootcfg.txt").read_text(encoding="utf-8") == "boot"
    assert (drive / "d12x.img").read_text(encoding="utf-8") == "img"
    assert not (drive / "程序信息.toml").exists()
    assert not (drive / program.name).exists()


def test_handcontrol_copy_stops_when_format_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program, drive = _program(tmp_path, {"a.rom": "rom", "a.pkg": "pkg"})
    calls: list[str] = []
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.format_usb",
        lambda _d, _log: calls.append("format") or False,
    )
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.eject_usb",
        lambda _d, _log: calls.append("eject") or True,
    )

    result = run_handcontrol_copy(str(drive), str(program), log_fn=lambda _m: None)

    assert calls == ["format"]
    assert result["ok"] is False
    assert result["code"] == "format_failed"
    assert list(drive.iterdir()) == []


def test_handcontrol_copy_stops_when_copy_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program, drive = _program(tmp_path, {"bootcfg.txt": "boot", "d12x.img": "img"})
    calls: list[str] = []
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.format_usb",
        lambda _d, _log: calls.append("format") or True,
    )
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.copy_named_files_to_usb",
        lambda *_args, **_kwargs: calls.append("copy") or False,
    )
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.eject_usb",
        lambda _d, _log: calls.append("eject") or True,
    )

    result = run_handcontrol_copy(str(drive), str(program), log_fn=lambda _m: None)

    assert calls == ["format", "copy"]
    assert result["ok"] is False
    assert result["code"] == "copy_failed"
    assert result["payload"]["ejected"] is False


def test_handcontrol_copy_does_not_format_without_firmware_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program, drive = _program(tmp_path, {"程序信息.toml": "vendor = 'x'\n"})
    calls: list[str] = []
    monkeypatch.setattr(
        "fwasset.core.services.flash_service.format_usb",
        lambda _d, _log: calls.append("format") or True,
    )

    result = run_handcontrol_copy(str(drive), str(program), log_fn=lambda _m: None)

    assert calls == []
    assert result["ok"] is False
    assert result["code"] == "files_missing"
