from pathlib import Path
from types import SimpleNamespace

import pytest

from fwasset.core.usb_ops import (
    copy_directory_to_usb,
    copy_to_usb,
    eject_usb,
    format_usb,
    get_usb_drives,
)


def _logs():
    messages = []
    return messages, messages.append


def test_get_usb_drives_filters_expected_partitions(monkeypatch: pytest.MonkeyPatch):
    parts = [
        SimpleNamespace(opts="rw,removable", fstype="NTFS", mountpoint="E:\\"),
        SimpleNamespace(opts="rw", fstype="FAT32", mountpoint="F:\\"),
        SimpleNamespace(opts="rw", fstype="NTFS", mountpoint="C:\\"),
    ]
    monkeypatch.setattr(
        "fwasset.core.usb_ops.psutil.disk_partitions", lambda all=False: parts
    )

    drives = get_usb_drives()

    assert drives == ["E:\\", "F:\\"]


def test_copy_to_usb_replaces_old_rom_pkg(tmp_path: Path):
    drive = tmp_path / "usb"
    drive.mkdir()
    old_rom = drive / "OLD.ROM"
    old_pkg = drive / "OLD.PKG"
    keep = drive / "note.txt"
    old_rom.write_text("old", encoding="utf-8")
    old_pkg.write_text("old", encoding="utf-8")
    keep.write_text("keep", encoding="utf-8")

    src = tmp_path / "src"
    src.mkdir()
    rom = src / "NEW.ROM"
    pkg = src / "NEW.PKG"
    rom.write_text("new rom", encoding="utf-8")
    pkg.write_text("new pkg", encoding="utf-8")

    logs, log_fn = _logs()
    ok = copy_to_usb(str(rom), str(pkg), str(drive), log_fn=log_fn)

    assert ok is True
    assert not old_rom.exists()
    assert not old_pkg.exists()
    assert (drive / "NEW.ROM").exists()
    assert (drive / "NEW.PKG").exists()
    assert keep.exists()
    assert any("移除旧文件" in msg for msg in logs)
    assert any("已复制" in msg for msg in logs)


def test_usb_copies_replace_read_only_leftovers(tmp_path: Path):
    """U 盘上上次复制留下的只读固件（copy2 保留属性）也要能替换。"""
    import os
    import stat

    drive = tmp_path / "usb"
    old_dir = drive / "music_A"
    old_dir.mkdir(parents=True)
    old_rom = drive / "OLD.ROM"
    old_rom.write_text("old", encoding="utf-8")
    (old_dir / "old.mp3").write_text("old", encoding="utf-8")
    os.chmod(old_rom, stat.S_IREAD)
    os.chmod(old_dir / "old.mp3", stat.S_IREAD)
    src = tmp_path / "src"
    (src / "music_A").mkdir(parents=True)
    (src / "music_A" / "a.mp3").write_text("new", encoding="utf-8")
    (src / "NEW.ROM").write_text("rom", encoding="utf-8")
    (src / "NEW.PKG").write_text("pkg", encoding="utf-8")

    _, log_fn = _logs()
    assert copy_to_usb(str(src / "NEW.ROM"), str(src / "NEW.PKG"), str(drive), log_fn=log_fn)
    assert copy_directory_to_usb(str(src / "music_A"), str(drive), log_fn=log_fn)

    assert not old_rom.exists()
    assert not (old_dir / "old.mp3").exists()
    assert (old_dir / "a.mp3").exists()


def test_copy_to_usb_returns_false_on_error(tmp_path: Path):
    drive = tmp_path / "usb"
    drive.mkdir()
    missing_rom = tmp_path / "missing.ROM"
    missing_pkg = tmp_path / "missing.PKG"

    logs, log_fn = _logs()
    ok = copy_to_usb(str(missing_rom), str(missing_pkg), str(drive), log_fn=log_fn)

    assert ok is False
    assert any("复制失败" in msg for msg in logs)


def test_copy_directory_to_usb_replace_existing(tmp_path: Path):
    drive = tmp_path / "usb"
    drive.mkdir()
    src = tmp_path / "music_A"
    src.mkdir()
    (src / "a.mp3").write_text("new", encoding="utf-8")

    old_target = drive / "music_A"
    old_target.mkdir()
    (old_target / "old.mp3").write_text("old", encoding="utf-8")

    logs, log_fn = _logs()
    ok = copy_directory_to_usb(str(src), str(drive), log_fn=log_fn)

    assert ok is True
    assert (drive / "music_A" / "a.mp3").exists()
    assert not (drive / "music_A" / "old.mp3").exists()
    assert any("已复制目录" in msg for msg in logs)


def test_eject_usb_success(monkeypatch: pytest.MonkeyPatch):
    logs, log_fn = _logs()

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "powershell"
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("fwasset.core.usb_ops.subprocess.run", fake_run)

    ok = eject_usb("E:\\", log_fn=log_fn)

    assert ok is True
    assert any("已安全弹出" in msg for msg in logs)


def test_eject_usb_failure_and_exception(monkeypatch: pytest.MonkeyPatch):
    logs1, log_fn1 = _logs()
    monkeypatch.setattr(
        "fwasset.core.usb_ops.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="denied"),
    )
    ok1 = eject_usb("E:\\", log_fn=log_fn1)
    assert ok1 is False
    assert any("弹出失败" in msg for msg in logs1)

    logs2, log_fn2 = _logs()

    def raise_run(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("fwasset.core.usb_ops.subprocess.run", raise_run)
    ok2 = eject_usb("E:\\", log_fn=log_fn2)
    assert ok2 is False
    assert any("弹出异常" in msg for msg in logs2)


def test_format_usb_success_failure_and_exception(monkeypatch: pytest.MonkeyPatch):
    # 正常：格式化成功 + 驱动器立即就绪
    logs1, log_fn1 = _logs()
    calls = []

    def run_success(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("fwasset.core.usb_ops.subprocess.run", run_success)
    monkeypatch.setattr("fwasset.core.usb_ops.time.sleep", lambda s: None)
    monkeypatch.setattr(
        "fwasset.core.usb_ops.Path.is_dir", lambda self: True
    )  # 驱动器就绪
    ok1 = format_usb("E:\\", log_fn=log_fn1)
    assert ok1 is True
    cmd = calls[0][0][0]
    assert cmd[0] == "powershell"
    assert "-NoProfile" in cmd
    assert "Format-Volume" in cmd[-1]
    assert "DriveLetter E" in cmd[-1]
    assert any("格式化完成" in msg for msg in logs1)

    # 失败：Format-Volume 返回非零
    logs2, log_fn2 = _logs()
    monkeypatch.setattr(
        "fwasset.core.usb_ops.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="failed", stdout=""
        ),
    )
    ok2 = format_usb("E:\\", log_fn=log_fn2)
    assert ok2 is False
    assert any("格式化失败" in msg for msg in logs2)

    # 异常：subprocess 抛出异常
    logs3, log_fn3 = _logs()

    def raise_run(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("fwasset.core.usb_ops.subprocess.run", raise_run)
    ok3 = format_usb("E:\\", log_fn=log_fn3)
    assert ok3 is False
    assert any("格式化异常" in msg for msg in logs3)


def test_format_usb_drive_ready_after_delay(monkeypatch: pytest.MonkeyPatch):
    """Issue 6: 驱动器延迟就绪，轮询第二次才返回 True。"""
    logs, log_fn = _logs()
    call_count = [0]

    def is_dir_delayed(self):
        call_count[0] += 1
        return call_count[0] >= 2  # 第二次调用才就绪

    monkeypatch.setattr(
        "fwasset.core.usb_ops.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=""),
    )
    monkeypatch.setattr("fwasset.core.usb_ops.time.sleep", lambda s: None)
    monkeypatch.setattr("fwasset.core.usb_ops.Path.is_dir", is_dir_delayed)
    ok = format_usb("E:\\", log_fn=log_fn)
    assert ok is True
    assert call_count[0] >= 2


def test_format_usb_drive_not_ready_timeout(monkeypatch: pytest.MonkeyPatch):
    """Issue 6: 驱动器始终未就绪，超时后返回 False。"""
    logs, log_fn = _logs()
    # 让 monotonic 快速进入超时：首次返回 0（设 deadline=10），之后返回 11
    mono_calls = [0]

    def mono_mock():
        mono_calls[0] += 1
        return 0.0 if mono_calls[0] == 1 else 11.0

    monkeypatch.setattr(
        "fwasset.core.usb_ops.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=""),
    )
    monkeypatch.setattr("fwasset.core.usb_ops.time.sleep", lambda s: None)
    monkeypatch.setattr("fwasset.core.usb_ops.time.monotonic", mono_mock)
    monkeypatch.setattr("fwasset.core.usb_ops.Path.is_dir", lambda self: False)
    ok = format_usb("E:\\", log_fn=log_fn)
    assert ok is False
    assert any("未重新就绪" in msg for msg in logs)


def test_format_usb_invalid_drive_letter(monkeypatch: pytest.MonkeyPatch):
    """Issue 7: 非单个英文字母盘符直接返回 False，不调用 subprocess。"""
    subprocess_called = [False]
    monkeypatch.setattr(
        "fwasset.core.usb_ops.subprocess.run",
        lambda *args, **kwargs: (
            subprocess_called.__setitem__(0, True)
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

    # 无效盘符：空串、数字开头、符号开头、非 ASCII 字母
    for bad_drive in ["", "1:\\", "!:\\", "中:\\"]:
        subprocess_called[0] = False
        logs, log_fn = _logs()
        ok = format_usb(bad_drive, log_fn=log_fn)
        assert ok is False, f"应拒绝无效盘符: {bad_drive!r}"
        assert not subprocess_called[0], f"不应调用 subprocess（drive={bad_drive!r}）"


def test_format_usb_timeout(monkeypatch: pytest.MonkeyPatch):
    logs, log_fn = _logs()

    def raise_timeout(*args, **kwargs):
        raise __import__("subprocess").TimeoutExpired(cmd="format", timeout=60)

    monkeypatch.setattr("fwasset.core.usb_ops.subprocess.run", raise_timeout)

    ok = format_usb("E:\\", log_fn=log_fn)

    assert ok is False
    assert any("格式化超时" in msg for msg in logs)


# ---------------------------------------------------------------------------
# D4.5：受管路径不得拷进 U 盘（TASK-20260905）
# ---------------------------------------------------------------------------


def test_copy_directory_to_usb_excludes_managed_paths(tmp_path: Path):
    drive = tmp_path / "usb"
    drive.mkdir()
    src = tmp_path / "主板程序-V1.0"
    (src / "子目录").mkdir(parents=True)
    (src / "firmware.bin").write_text("fw", encoding="utf-8")
    (src / "子目录" / "extra.bin").write_text("extra", encoding="utf-8")
    (src / "程序信息.toml").write_text("vendor = 'x'", encoding="utf-8")
    (src / "旧版本" / "主板-V0.9").mkdir(parents=True)
    (src / "旧版本" / "主板-V0.9" / "old.bin").write_text("old", encoding="utf-8")

    messages, log = _logs()
    assert copy_directory_to_usb(str(src), str(drive), log) is True

    target = drive / src.name
    assert (target / "firmware.bin").read_text(encoding="utf-8") == "fw"
    assert (target / "子目录" / "extra.bin").read_text(encoding="utf-8") == "extra"
    assert not (target / "旧版本").exists()
    assert not (target / "程序信息.toml").exists()


def test_copy_directory_to_usb_excludes_nested_retired_versions(tmp_path: Path):
    drive = tmp_path / "usb"
    drive.mkdir()
    src = tmp_path / "程序"
    (src / "变体A" / "旧版本" / "v1").mkdir(parents=True)
    (src / "变体A" / "旧版本" / "v1" / "old.bin").write_text("old", encoding="utf-8")
    (src / "变体A" / "cur.bin").write_text("cur", encoding="utf-8")

    messages, log = _logs()
    assert copy_directory_to_usb(str(src), str(drive), log) is True

    target = drive / src.name
    assert (target / "变体A" / "cur.bin").exists()
    assert not (target / "变体A" / "旧版本").exists()


def test_copy_directory_to_usb_keeps_lookalike_names(tmp_path: Path):
    """前缀目录与同名文件不得误排。"""
    drive = tmp_path / "usb"
    drive.mkdir()
    src = tmp_path / "程序"
    (src / "旧版本说明").mkdir(parents=True)
    (src / "旧版本说明" / "readme.txt").write_text("doc", encoding="utf-8")
    (src / "旧版本").write_text("这是文件不是目录", encoding="utf-8")

    messages, log = _logs()
    assert copy_directory_to_usb(str(src), str(drive), log) is True

    target = drive / src.name
    assert (target / "旧版本说明" / "readme.txt").exists()
    assert (target / "旧版本").is_file()
