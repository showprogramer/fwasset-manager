"""TASK-20260901 R3 行级写 API 人工验证场景（不经 UI，直接驱动 core API）。

在隔离的 .scenario/r3/ 目录搭建迷你固件工作区 + 独立 SQLite 索引库，
不触碰本机真实固件目录与 fwasset.db。

用法（Windows PowerShell，开发模式）:

    uv run python scripts\\verify_r3_write_api.py

四类场景：
  1. 新增     upsert_asset（重扫子树生成完整资产后写库）
  2. 重命名   replace_asset（目录改名 → 完整新行替换，不是只改 path）
  3. 删除     reconcile_subtree（已删子树清空）+ delete_asset（单行）
  4. 失败恢复 SQL 触发器注入写库失败 → reconcile_subtree 恢复一致
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from fwasset.core.asset_index import (
    AssetIndexError,
    connect_asset_index,
    delete_asset,
    load_assets,
    load_hidden_items,
    replace_asset,
    save_assets,
    upsert_asset,
)
from fwasset.core.asset_reconcile import reconcile_subtree
from fwasset.core.file_scan import scan_firmware_assets, scan_firmware_subtree
from fwasset.core.types import ScanIssue

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIO = REPO_ROOT / ".scenario" / "r3"
WS = SCENARIO / "工作区"
DB = SCENARIO / "fwasset.db"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"（{detail}）" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def write_bin(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("rom", encoding="utf-8")


def paths_in_db() -> set[str]:
    return {a["path"] for a in load_assets(DB)}


def add_fail_trigger(fail_path: str) -> None:
    literal = fail_path.replace("'", "''")
    conn = connect_asset_index(DB)
    try:
        conn.execute(
            f"CREATE TRIGGER fail_insert BEFORE INSERT ON assets"
            f" WHEN NEW.path = '{literal}' BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        conn.commit()
    finally:
        conn.close()


def drop_fail_trigger() -> None:
    conn = connect_asset_index(DB)
    try:
        conn.execute("DROP TRIGGER IF EXISTS fail_insert")
        conn.commit()
    finally:
        conn.close()


def error_messages(issues: list[ScanIssue]) -> list[str]:
    """TASK-20260916 分级语义：只有 error 级 issue 才视为扫描失败。"""
    return [
        str(issue.get("message"))
        for issue in issues
        if issue.get("severity") == "error"
    ]


def main() -> int:
    if SCENARIO.exists():
        shutil.rmtree(SCENARIO)
    WS.mkdir(parents=True)

    # ---- 准备：两型号工作区 + 全量扫描播种 ----
    v1 = WS / "L36主板程序" / "主板程序_v1"
    l50 = WS / "L50主板程序" / "主板程序_v1"
    write_bin(v1 / "ITE_NOR_L36_v1.0.0.bin")
    write_bin(l50 / "ITE_NOR_L50_v1.0.0.bin")
    assets, issues = scan_firmware_assets(str(WS))
    assert not error_messages(issues), issues
    save_assets(assets, str(WS), DB, scanned_at=1000.0)
    print(f"准备：工作区 {WS}")
    print(f"      索引库 {DB}（播种 {len(assets)} 行）")

    # ---- 场景 1：新增（upsert_asset）----
    print("\n场景 1：新增程序目录 → 子树扫描 → upsert_asset")
    v2 = WS / "L36主板程序" / "主板程序_v2"
    write_bin(v2 / "ITE_NOR_L36_v2.0.0.bin")
    sub_assets, sub_issues = scan_firmware_subtree(str(WS), str(WS / "L36主板程序"))
    check("子树扫描无错误", not error_messages(sub_issues))
    new_asset = next(a for a in sub_assets if a["path"] == str(v2))
    upsert_asset(new_asset, path=DB)
    check("新行已入库（字段级）", new_asset in load_assets(DB))
    check("新增不影响 scan_meta 工作区根", len(load_assets(DB)) == 3)

    # ---- 场景 2：重命名（replace_asset）----
    print("\n场景 2：目录重命名 → 子树扫描 → replace_asset 整行替换")
    renamed = WS / "L36主板程序" / "主板程序_v2_改名"
    v2.rename(renamed)
    sub_assets, _ = scan_firmware_subtree(str(WS), str(WS / "L36主板程序"))
    renamed_asset = next(a for a in sub_assets if a["path"] == str(renamed))
    check(
        "重扫生成的完整资产字段已变化",
        renamed_asset["directory_name"] == "主板程序_v2_改名"
        and renamed_asset["path"] != new_asset["path"],
    )
    replace_asset(new_asset["path"], renamed_asset, path=DB)
    check("旧行已删除", new_asset["path"] not in paths_in_db())
    check("新行为完整行（非仅改 path）", renamed_asset in load_assets(DB))

    # ---- 场景 3：删除 ----
    print("\n场景 3a：目录删除 → reconcile_subtree 清空该子树")
    shutil.rmtree(renamed)
    sibling_before = [a for a in load_assets(DB) if "L50" in a["path"]]
    reconcile_subtree(str(WS), str(renamed), path=DB)
    check("已删子树的行被清空", str(renamed) not in paths_in_db())
    check(
        "兄弟子树（L50）逐行不变",
        [a for a in load_assets(DB) if "L50" in a["path"]] == sibling_before,
    )

    print("\n场景 3b：delete_asset 单行删除（含隐藏项）")
    l50_asset = sibling_before[0]
    from fwasset.core.asset_index import hide_item

    hide_item(l50_asset["path"], "asset", path=DB)
    check("delete_asset 返回 True", delete_asset(l50_asset["path"], path=DB) is True)
    check("行已删除", l50_asset["path"] not in paths_in_db())
    check("asset 级隐藏行一并删除", str(l50_asset["path"]) not in load_hidden_items(DB))
    check("重复删除幂等返回 False", delete_asset(l50_asset["path"], path=DB) is False)

    # ---- 场景 4：失败后对账恢复 ----
    print("\n场景 4：SQL 触发器注入写库失败 → reconcile_subtree 恢复一致")
    l50_v2 = WS / "L50主板程序" / "主板程序_v2"
    write_bin(l50_v2 / "ITE_NOR_L50_v2.0.0.bin")
    sub_assets, _ = scan_firmware_subtree(str(WS), str(WS / "L50主板程序"))
    l50_new = next(a for a in sub_assets if a["path"] == str(l50_v2))
    count_before = len(load_assets(DB))
    add_fail_trigger(l50_new["path"])
    try:
        upsert_asset(l50_new, path=DB)
        check("注入失败生效（应抛 AssetIndexError）", False)
    except AssetIndexError as exc:
        check("写库失败抛 AssetIndexError", True, str(exc))
    check(
        f"失败后库未被破坏（仍 {count_before} 行）",
        len(load_assets(DB)) == count_before,
    )
    drop_fail_trigger()
    reconcile_subtree(str(WS), str(WS / "L50主板程序"), path=DB)
    full_scan, full_issues = scan_firmware_assets(str(WS))
    check(
        "对账后整库与磁盘一致（字段级）",
        not error_messages(full_issues) and load_assets(DB) == full_scan,
    )

    # ---- 汇总 ----
    print("\n========== 验证结果 ==========")
    if failures:
        print(f"失败 {len(failures)} 项：{'、'.join(failures)}")
        return 1
    print("四类场景全部通过（OK）：新增 / 重命名 / 删除 / 失败后对账恢复")
    print(f"现场保留在 {SCENARIO}，可人工核查后删除")
    return 0


if __name__ == "__main__":
    sys.exit(main())
