"""TASK-20260915 系列任务隔离场景验证。

场景 1–5（事务状态基础）：新增事务、写入期硬退出、中断/恢复阻写。
场景 6（原子目录原语）：staging 分配 → 原子提升 → 产物记录 → 删除产物。
在独立临时目录中执行，不触碰真实工作区。本模块无 UI 入口，按无 UI
等效规则以脚本完成场景验证，供人工复核与回归复跑。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fwasset.core.staging_io import (
    allocate_staging_area,
    delete_recorded_product,
    promote_staging,
)
from fwasset.core.workspace_transaction import (
    WorkspaceRecoveryRequiredError,
    WorkspaceTransaction,
    load_operation_log,
    load_workspace_status,
    recover_interrupted_workspace,
)


def state_files(workspace: Path) -> dict[str, object]:
    state_dir = workspace / ".fwasset" / "state"
    return {
        p.name: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(state_dir.iterdir())
        if p.suffix == ".json"
    }


def expect_blocked(workspace: Path, operation: str) -> str:
    try:
        with WorkspaceTransaction(workspace, operation=operation):
            raise AssertionError("不应进入事务体")
    except WorkspaceRecoveryRequiredError as exc:
        return str(exc)
    raise AssertionError("未抛出 WorkspaceRecoveryRequiredError")


def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="fwasset-verify-wt-"))
    workspace = base / "workspace"
    workspace.mkdir()
    print(f"隔离工作区: {workspace}")

    with WorkspaceTransaction(workspace, operation="verify-create") as tx:
        tx.begin_product_write()
        tx.commit()
    status = load_workspace_status(workspace)
    print(f"场景1 新增事务提交后: {status}")
    print(f"场景1 状态目录: {state_files(workspace)}")
    assert status.state == "clean"
    assert status.generation == 2
    assert status.operation is None

    crash = (
        "import os\n"
        "from fwasset.core.workspace_transaction import WorkspaceTransaction\n"
        f"with WorkspaceTransaction(r'{workspace}', operation='verify-crash') as tx:\n"
        "    tx.begin_product_write()\n"
        "    os._exit(1)\n"
    )
    result = subprocess.run([sys.executable, "-c", crash])
    assert result.returncode == 1, "子进程应模拟写入期硬退出"
    status = load_workspace_status(workspace)
    print(f"场景2 写入期硬退出后: {status}")
    print(f"场景2 状态目录: {state_files(workspace)}")
    assert status.state == "operation_in_progress"
    assert status.generation == 3
    assert status.operation == "verify-crash"

    message = expect_blocked(workspace, "verify-blocked")
    print(f"场景3 中断现场阻止新写事务: {message}")

    status = recover_interrupted_workspace(workspace)
    print(f"场景4 恢复流程后: {status}")
    print(f"场景4 状态目录: {state_files(workspace)}")
    assert status.state == "recovery_required"
    assert status.generation == 4
    assert status.operation == "verify-crash"

    message = expect_blocked(workspace, "verify-blocked-2")
    print(f"场景5 recovery_required 下写事务仍被阻止: {message}")

    second = base / "workspace2"
    second.mkdir()
    with WorkspaceTransaction(second, operation="verify-atomic") as tx:
        area = allocate_staging_area(second, tx)
        (area / "main.rom").write_bytes(b"ROM")
        (area / "子").mkdir()
        (area / "子" / "extra.pkg").write_bytes(b"PKG")

        target = second / "通用" / "主板" / "demo"
        target.parent.mkdir(parents=True)
        tx.begin_product_write()
        promote_staging(tx, second, area, target)

        assert (target / "main.rom").read_bytes() == b"ROM"
        assert not area.exists()

        log = load_operation_log(second)
        assert log is not None
        recorded = [
            product
            for product in log["products"]
            if os.path.normcase(product["path"]) == os.path.normcase(str(target))
        ]
        assert len(recorded) == 1
        assert recorded[0]["manifest"]
        print(f"场景6 提升后日志产物: {recorded[0]}")

        delete_recorded_product(tx, second, target)
        assert not target.exists()
        target.parent.rmdir()
        target.parent.parent.rmdir()
        tx.commit()

    status = load_workspace_status(second)
    print(f"场景6 删除产物并提交后: {status}")
    assert status.state == "clean"
    assert status.generation == 2
    assert status.operation is None

    print("全部断言通过")


if __name__ == "__main__":
    main()
