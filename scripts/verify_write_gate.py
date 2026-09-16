"""TASK-20260806 R1/R10 写入门闩人工验证场景。

在隔离的 .scenario/ 目录搭建两套迷你固件根 + 独立 runtime，不触碰本机
真实固件目录；配置写入前自动备份、结束后 restore，不会破坏既有 config。

用法（Windows PowerShell，开发模式）:

    .\\.venv\\Scripts\\python.exe scripts\\verify_write_gate.py

冻结 exe（需提供 exe 路径，配置写 %APPDATA%\\fwasset\\config.toml）:

    .\\.venv\\Scripts\\python.exe scripts\\verify_write_gate.py --exe D:\\fwasset.exe

分步执行（prepare / phase1 / phase2 / phase3 / restore）:

    .\\.venv\\Scripts\\python.exe scripts\\verify_write_gate.py prepare
    ... 每步之间可中断，随时可 restore
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO = REPO_ROOT / ".scenario"
ROOT_A = SCENARIO / "程序根A"
ROOT_B = SCENARIO / "程序根B"
RUNTIME = SCENARIO / "runtime"
DEV_CONFIG = REPO_ROOT / "config.toml"
BACKUP_SUFFIX = ".verify-bak"

PLATFORM_CONFIG_A = """\
[[platform]]
name = "标准单机芯3D"
[platform.defaults]
"主板程序" = "量产_默认"
"手控UI" = "中文-通用_默认"
"""

PLATFORM_CONFIG_B = """\
[[platform]]
name = "标准单机芯3D"
[platform.defaults]
"主板程序" = "量产_默认"
"""

SCHEME_CONFIG = """\
name = "葡萄牙-小明"
platform = "标准单机芯3D"
"""


def _active_config_path(exe: str | None) -> Path:
    if exe:
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "fwasset" / "config.toml"
    return DEV_CONFIG


def _write_config(root_dir: str, exe: str | None) -> Path:
    config_path = _active_config_path(exe)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    root = root_dir.replace("\\", "/")
    config_path.write_text(
        f'[paths]\nroot_dir = "{root}"\ntool_root = ""\n', encoding="utf-8"
    )
    print(f"[config] 写入 {config_path}  root_dir = {root_dir!r}")
    return config_path


def _backup_config(exe: str | None) -> None:
    config_path = _active_config_path(exe)
    backup = Path(str(config_path) + BACKUP_SUFFIX)
    if config_path.exists() and not backup.exists():
        shutil.copy2(config_path, backup)
        print(f"[backup] 已备份 {config_path} → {backup}")


def restore_config(exe: str | None) -> None:
    config_path = _active_config_path(exe)
    backup = Path(str(config_path) + BACKUP_SUFFIX)
    if backup.exists():
        shutil.move(str(backup), str(config_path))
        print(f"[restore] 已恢复 {config_path}（原配置）")
    else:
        print(f"[restore] 无备份（{backup} 不存在），跳过")


def _write_file(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)


def build_tree(root: Path, platform_config: str, *, with_custom: bool) -> None:
    if root.exists():
        shutil.rmtree(root)
    _write_file(root / "平台配置.toml", platform_config)
    _write_file(root / "通用" / "主板程序" / "量产_默认" / "L36_V1.0.bin", b"fw")
    _write_file(root / "通用" / "主板程序" / "备用_V2.1" / "L36_V2.1.bin", b"fw")
    _write_file(root / "通用" / "手控UI" / "中文-通用_默认" / "L36_V1.0.rom", b"fw")
    _write_file(root / "通用" / "手控UI" / "中文-通用_默认" / "L36_V1.0.pkg", b"fw")
    _write_file(root / "通用" / "蓝牙程序" / "中文-通用_默认" / "L36_V1.0.bin", b"fw")
    if with_custom:
        _write_file(root / "定制" / "葡萄牙-小明" / "方案配置.toml", SCHEME_CONFIG)
        _write_file(
            root / "定制" / "葡萄牙-小明" / "主板程序" / "定制板_V1.0" / "PT_V1.0.bin",
            b"fw",
        )
    print(f"[tree] 已构建 {root}")


def prepare(exe: str | None) -> None:
    _backup_config(exe)
    build_tree(ROOT_A, PLATFORM_CONFIG_A, with_custom=True)
    build_tree(ROOT_B, PLATFORM_CONFIG_B, with_custom=False)
    if RUNTIME.exists():
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    print(f"[runtime] 已重置 {RUNTIME}（保证 Phase 1 从干净缓存开始）")


def launch_app(exe: str | None) -> int:
    env = dict(os.environ)
    env["FWASSET_RUNTIME_DIR"] = str(RUNTIME)
    if exe:
        cmd = [exe]
    else:
        cmd = ["uv", "run", "fwasset"]
    print(f"[launch] {' '.join(cmd)}  （FWASSET_RUNTIME_DIR={RUNTIME}）")
    print("[launch] 应用运行中，请在界面完成本阶段验证后关闭应用返回本脚本…")
    return subprocess.call(cmd, cwd=str(REPO_ROOT), env=env)


def _checklist(items: list[str]) -> None:
    print("\n【本阶段人工验证清单】")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    print()


PHASE1_STEPS = [
    "资产树出现：通用模块（主板程序：量产_默认/备用_V2.1、手控UI、蓝牙程序）、定制方案「葡萄牙-小明」",
    "「量产_默认」行显示 ★默认 徽章（平台配置默认变体）",
    "右键「量产_默认」→ 菜单**不含**「设为默认」项（已默认），只含「登记共享来源…」",
    "右键「主板程序 / 备用_V2.1」→ 菜单包含「设为…默认版本」和「登记共享来源…」",
    "对「备用_V2.1」执行「设为默认」→ 提示成功后其行出现 ★默认 徽章",
    "左侧「重新读取程序文件夹」→ 资产树不变、写入口仍可用",
    "结论：配置正常时写入口可用",
]

PHASE2_STEPS = [
    "日志出现「使用上次读取的程序文件夹: …程序根A」（配置为空，从缓存恢复浏览）",
    "资产树仍显示上一阶段的资产（只读浏览缓存）",
    "右键任意变体 → 菜单只有「打开所在目录」「复制目录路径」，无任何写操作项",
    "左侧「设置」显示「未配置」",
    "结论：配置为空 + 有旧缓存 → 只读浏览、全部写入口被禁用",
]

PHASE3_STEPS = [
    "启动后资产树与 Phase 1 一致，右键「备用_V2.1」→ 写入口恢复可用",
    "「设置」→ 修改程序文件夹为 .scenario\\程序根B → 确认切换 → 重新读取完成",
    "重新读取期间（转圈/日志进行中）右键 → 写操作项不可用（扫描进行中拦截）",
    "读取完成后资产树变为 程序根B 的内容，右键写入口恢复",
    "结论：配置重新读取后写入口恢复；扫描中写入口被拦截",
]


def phase1(exe: str | None) -> int:
    _write_config(str(ROOT_A), exe)
    _checklist(PHASE1_STEPS)
    return launch_app(exe)


def phase2(exe: str | None) -> int:
    _write_config("", exe)
    _checklist(PHASE2_STEPS)
    return launch_app(exe)


def phase3(exe: str | None) -> int:
    _write_config(str(ROOT_A), exe)
    _checklist(PHASE3_STEPS)
    return launch_app(exe)


def main() -> int:
    parser = argparse.ArgumentParser(description="R1/R10 写入门闩人工验证场景")
    parser.add_argument(
        "step",
        nargs="?",
        default="all",
        choices=["all", "prepare", "phase1", "phase2", "phase3", "restore"],
    )
    parser.add_argument(
        "--exe", default=None, help="冻结 exe 路径（缺省用 uv run fwasset）"
    )
    args = parser.parse_args()

    steps: list[str]
    if args.step == "all":
        steps = ["prepare", "phase1", "phase2", "phase3"]
    elif args.step == "prepare":
        steps = ["prepare"]
    else:
        steps = [args.step]

    exit_code = 0
    try:
        for step in steps:
            if step == "prepare":
                prepare(args.exe)
            elif step in ("phase1", "phase2", "phase3"):
                # 分步执行时备份可能尚未建立（prepare 单独跑过且已还原），幂等补齐
                _backup_config(args.exe)
                if step == "phase1":
                    exit_code = phase1(args.exe)
                elif step == "phase2":
                    exit_code = phase2(args.exe)
                else:
                    exit_code = phase3(args.exe)
            elif step == "restore":
                restore_config(args.exe)
            if args.step == "all" and step != "prepare":
                print(f"\n【Phase {step[-1]} 应用已关闭，退出码 {exit_code}】")
                answer = (
                    input("该阶段是否验证通过？(y/n，回车默认 y): ").strip().lower()
                )
                if answer == "n":
                    print("验证未通过，中止并还原配置。")
                    restore_config(args.exe)
                    return 1
        if args.step == "all":
            restore_config(args.exe)
        elif "restore" not in steps:
            print("[note] 分步模式不自动还原配置；验证完毕后请执行 restore 步骤还原。")
        if args.step == "all":
            print("\n全部阶段完成。配置已还原，可正常使用本机配置。")
        return exit_code
    except KeyboardInterrupt:
        print("\n已中断，正在还原配置…")
        restore_config(args.exe)
        return 130


if __name__ == "__main__":
    sys.exit(main())
