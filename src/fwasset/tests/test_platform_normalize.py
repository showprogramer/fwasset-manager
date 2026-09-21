"""D5.5 存量 platform 归一：预览、follow_default 前置、执行与低层 API 收紧。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import fwasset.core.services.platform_normalize_service as pns
from fwasset.core.model_config import SharedModuleRef, save_shared_module
from fwasset.core.platform_config import (
    PLATFORM_CONFIG_FILENAME,
    PlatformDefaults,
    load_platform_config_with_status,
    save_platform_config,
)
from fwasset.core.services.model_scheme_service import create_model
from fwasset.core.services.platform_default_service import (
    ensure_platform_blocks,
    set_default_variant,
)
from fwasset.core.services.platform_normalize_service import (
    migrate_follow_defaults_for_normalize,
    normalize_platform_config,
    preview_platform_normalize,
)
from fwasset.core.services.reference_service import migrate_follow_default_refs
from fwasset.core.types import (
    NormalizeExpectation,
    PlatformNormalizePreview,
    ServiceResult,
)
from fwasset.core.workspace_transaction import (
    WorkspaceTransaction,
    load_workspace_status,
)


def _model(workspace: Path, name: str) -> Path:
    result = create_model(str(workspace), str(workspace), name, "单3D")
    assert result["ok"], result
    return workspace / name


def _config(model_root: Path) -> Path:
    return model_root / PLATFORM_CONFIG_FILENAME


def _write_blocks(model_root: Path, blocks: list[tuple[str, dict[str, str]]]) -> None:
    save_platform_config(
        model_root, [PlatformDefaults(name, dict(defaults)) for name, defaults in blocks]
    )


def _preview(workspace: Path, model_root: Path) -> PlatformNormalizePreview:
    result = preview_platform_normalize(str(workspace), str(workspace), str(model_root))
    assert result["ok"], result
    return cast(PlatformNormalizePreview, result["payload"]["preview"])


def _asset(model_root: Path, module: str, name: str) -> Path:
    root = model_root / "通用" / module / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "fw.bin").write_bytes(b"firmware")
    return root


def _conflict(preview: PlatformNormalizePreview, module_key: str) -> dict[str, Any]:
    matches = [c for c in preview["conflicts"] if c["module_key"] == module_key]
    assert len(matches) == 1, preview["conflicts"]
    return cast(dict[str, Any], matches[0])


def _assert_clean(workspace: Path) -> None:
    status = load_workspace_status(workspace)
    assert status.state == "clean"
    assert status.generation % 2 == 0


class TestPreview:
    def test_multiple_legacy_blocks(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("标准单机芯3D", {"语音板": "乙"})])

        preview = _preview(tmp_path, model)

        assert [b["name"] for b in preview["blocks"]] == ["默认", "标准单机芯3D"]
        assert [b["block_index"] for b in preview["blocks"]] == [0, 1]
        assert all(b["is_chassis_type"] is False for b in preview["blocks"])
        assert preview["chassis_candidates"] == []
        assert preview["already_normalized"] is False

    def test_enum_block_name_becomes_candidate(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"主板": "甲"}), ("默认", {"语音板": "乙"})])

        preview = _preview(tmp_path, model)

        assert preview["chassis_candidates"] == ["单3D"]
        assert preview["blocks"][0]["is_chassis_type"] is True

    def test_same_value_across_blocks_is_unique(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"主板": "甲"})])

        preview = _preview(tmp_path, model)

        conflict = _conflict(preview, "主板")
        assert conflict["kind"] == "unique"
        assert conflict["resolved"] == "甲"
        assert conflict["raw_keys"] == ["主板"]
        assert preview["needs_module_choice"] == []

    def test_different_value_across_blocks_is_conflict(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "乙"}), ("旧块", {"主板": "甲"})])

        preview = _preview(tmp_path, model)

        conflict = _conflict(preview, "主板")
        assert conflict["kind"] == "value_conflict"
        assert conflict["values"] == ["乙", "甲"] or conflict["values"] == sorted(["甲", "乙"])
        assert conflict["resolved"] == ""
        assert preview["needs_module_choice"] == ["主板"]

    def test_alias_same_value_in_one_block(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"机芯版": "甲", "机芯板": "甲"})])

        preview = _preview(tmp_path, model)

        conflict = _conflict(preview, "机芯板")
        assert conflict["kind"] == "alias_duplicate"
        assert conflict["raw_keys"] == ["机芯板", "机芯版"]
        assert conflict["resolved"] == "甲"
        assert preview["already_normalized"] is False
        assert preview["needs_module_choice"] == []

    def test_alias_same_value_across_blocks(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"机芯版": "甲"}), ("旧块", {"机芯板": "甲"})])

        preview = _preview(tmp_path, model)

        assert _conflict(preview, "机芯板")["kind"] == "alias_duplicate"

    def test_alias_different_value_is_conflict(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"机芯版": "甲", "机芯板": "乙"})])

        preview = _preview(tmp_path, model)

        assert _conflict(preview, "机芯板")["kind"] == "value_conflict"

    def test_empty_string_participates_in_conflict(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": ""}), ("旧块", {"主板": "甲"})])

        preview = _preview(tmp_path, model)

        conflict = _conflict(preview, "主板")
        assert conflict["kind"] == "value_conflict"
        assert "" in conflict["values"]

    def test_already_normalized(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"主板": "甲"})])

        preview = _preview(tmp_path, model)

        assert preview["already_normalized"] is True
        assert [c["kind"] for c in preview["conflicts"]] == ["unique"]

    def test_single_enum_block_with_alias_is_not_normalized(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"机芯版": "甲", "机芯板": "甲"})])

        assert _preview(tmp_path, model)["already_normalized"] is False

    def test_preview_does_not_hold_lock(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"})])

        preview_platform_normalize(str(tmp_path), str(tmp_path), str(model))

        with WorkspaceTransaction(tmp_path, operation="probe") as transaction:
            transaction.commit()
        _assert_clean(tmp_path)

    def test_preview_is_zero_write(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"主板": "乙"})])
        before = _config(model).read_bytes()
        generation = load_workspace_status(tmp_path).generation

        _preview(tmp_path, model)

        assert _config(model).read_bytes() == before
        assert load_workspace_status(tmp_path).generation == generation


class TestDiscardedContent:
    def test_counts_three_comment_forms(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            "# 行首注释\n"
            "[[platform]]\n"
            '  # 缩进注释\n'
            'name = "默认"  # 行尾注释\n'
            "[platform.defaults]\n"
            '"主板" = "甲"\n',
            encoding="utf-8",
        )

        preview = _preview(tmp_path, model)

        assert "注释 3 处" in preview["discarded_content"]

    def test_app_header_at_file_start_is_not_counted(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"})])

        preview = _preview(tmp_path, model)

        assert not any("注释" in item for item in preview["discarded_content"])

    def test_similar_but_unequal_header_is_counted(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            "# 本文件由 fwasset 管理\n"
            "[[platform]]\n"
            'name = "默认"\n'
            "[platform.defaults]\n"
            '"主板" = "甲"\n',
            encoding="utf-8",
        )

        preview = _preview(tmp_path, model)

        assert "注释 1 处" in preview["discarded_content"]

    def test_partial_app_header_is_fully_counted(self, tmp_path: Path) -> None:
        """只有第一行文件头时不构成应用头：它可能是用户复制的，必须计入。"""
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            "# 本文件由 fwasset 管理（工作台「设为平台默认」会改写它）。\n"
            "[[platform]]\n"
            'name = "默认"\n'
            "[platform.defaults]\n"
            '"主板" = "甲"\n',
            encoding="utf-8",
        )

        preview = _preview(tmp_path, model)

        assert "注释 1 处" in preview["discarded_content"]

    def test_app_header_out_of_order_is_counted(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            "# defaults 键 = 通用区模块目录名，值 = 默认变体子目录名（空串表示该模块唯一）。\n"
            "# 本文件由 fwasset 管理（工作台「设为平台默认」会改写它）。\n"
            "[[platform]]\n"
            'name = "默认"\n'
            "[platform.defaults]\n"
            '"主板" = "甲"\n',
            encoding="utf-8",
        )

        preview = _preview(tmp_path, model)

        assert "注释 2 处" in preview["discarded_content"]

    def test_app_header_as_trailing_comment_is_counted(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            'x = "y"  # 本文件由 fwasset 管理（工作台「设为平台默认」会改写它）。\n'
            "# defaults 键 = 通用区模块目录名，值 = 默认变体子目录名（空串表示该模块唯一）。\n"
            "[[platform]]\n"
            'name = "默认"\n'
            "[platform.defaults]\n"
            '"主板" = "甲"\n',
            encoding="utf-8",
        )

        preview = _preview(tmp_path, model)

        assert "注释 2 处" in preview["discarded_content"]

    def test_header_text_in_mid_file_is_counted(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            "# 用户自己的第一行注释\n"
            "# 本文件由 fwasset 管理（工作台「设为平台默认」会改写它）。\n"
            "# defaults 键 = 通用区模块目录名，值 = 默认变体子目录名（空串表示该模块唯一）。\n"
            "[[platform]]\n"
            'name = "默认"\n'
            "[platform.defaults]\n"
            '"主板" = "甲"\n',
            encoding="utf-8",
        )

        preview = _preview(tmp_path, model)

        assert "注释 3 处" in preview["discarded_content"]

    @pytest.mark.parametrize(
        "literal",
        ['"a#b"', "'a#b'", '"""a#b"""', "'''a#b'''"],
    )
    def test_hash_inside_string_is_not_a_comment(self, tmp_path: Path, literal: str) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            "[[platform]]\n"
            f"name = {literal}\n"
            "[platform.defaults]\n"
            '"主板" = "甲"\n',
            encoding="utf-8",
        )

        preview = _preview(tmp_path, model)

        assert not any("注释" in item for item in preview["discarded_content"])

    def test_unknown_fields_listed(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            'schema_version = "2"\n'
            "[[platform]]\n"
            'name = "默认"\n'
            'note = "备注"\n'
            "[platform.defaults]\n"
            '"主板" = "甲"\n',
            encoding="utf-8",
        )

        preview = _preview(tmp_path, model)

        assert "顶层字段「schema_version」" in preview["discarded_content"]
        assert "块「默认」的字段「note」" in preview["discarded_content"]

    def test_empty_when_nothing_discarded(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"主板": "甲"})])

        assert _preview(tmp_path, model)["discarded_content"] == []

    def test_discarded_content_does_not_block_normalize(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            "# 用户注释\n"
            'schema_version = "2"\n'
            "[[platform]]\n"
            'name = "默认"\n'
            "[platform.defaults]\n"
            '"主板" = "甲"\n',
            encoding="utf-8",
        )

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["ok"] is True, result
        text = _config(model).read_text(encoding="utf-8")
        assert "用户注释" not in text
        assert "schema_version" not in text


class TestFollowDefaultGate:
    def _borrower_follow_default(
        self, workspace: Path, source: Path, borrower: Path, module: str = "语音板"
    ) -> None:
        asset = _asset(source, module, "程序A")
        assert asset.exists()
        from fwasset.core.model_config import load_model_config

        source_id, status, _err = load_model_config(source)
        assert status == "ok", status
        save_shared_module(
            borrower,
            SharedModuleRef(
                module_key=module,
                source_model_id=source_id,
                source_group="通用",
                source_module=module,
                source_relative_path=f"{source.name}/通用/{module}",
                mode="follow_default",
                source_platform="默认",
            ),
        )

    def test_inbound_follow_default_blocks(self, tmp_path: Path) -> None:
        source = _model(tmp_path, "L36程序")
        borrower = _model(tmp_path, "L37程序")
        _write_blocks(source, [("默认", {"语音板": "程序A"}), ("旧块", {"主板": "甲"})])
        self._borrower_follow_default(tmp_path, source, borrower)

        preview = _preview(tmp_path, source)
        assert preview["follow_default_blocked"] is True
        assert preview["follow_default_hits"]

        before = _config(source).read_bytes()
        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(source), "单3D", log_fn=lambda _m: None
        )

        assert result["code"] == "follow_default_migration_required"
        assert _config(source).read_bytes() == before
        _assert_clean(tmp_path)

    def test_outbound_follow_default_does_not_block(self, tmp_path: Path) -> None:
        source = _model(tmp_path, "L36程序")
        borrower = _model(tmp_path, "L37程序")
        _write_blocks(source, [("默认", {"语音板": "程序A"})])
        _write_blocks(borrower, [("默认", {"主板": "甲"}), ("旧块", {"主板": "甲"})])
        self._borrower_follow_default(tmp_path, source, borrower)

        preview = _preview(tmp_path, borrower)
        assert preview["follow_default_blocked"] is False

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(borrower), "单3D", log_fn=lambda _m: None
        )
        assert result["ok"] is True, result

    def test_unrelated_models_do_not_block(self, tmp_path: Path) -> None:
        target = _model(tmp_path, "L36程序")
        source = _model(tmp_path, "L38程序")
        borrower = _model(tmp_path, "L39程序")
        _write_blocks(target, [("默认", {"主板": "甲"}), ("旧块", {"主板": "甲"})])
        _write_blocks(source, [("默认", {"语音板": "程序A"})])
        self._borrower_follow_default(tmp_path, source, borrower)

        assert _preview(tmp_path, target)["follow_default_blocked"] is False
        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(target), "单3D", log_fn=lambda _m: None
        )
        assert result["ok"] is True, result

    def test_migration_unblocks_normalize(self, tmp_path: Path) -> None:
        source = _model(tmp_path, "L36程序")
        borrower = _model(tmp_path, "L37程序")
        _write_blocks(source, [("默认", {"语音板": "程序A"}), ("旧块", {"主板": "甲"})])
        self._borrower_follow_default(tmp_path, source, borrower)

        migrated = migrate_follow_defaults_for_normalize(
            str(tmp_path), str(tmp_path), log_fn=lambda _m: None
        )
        assert migrated["ok"] is True, migrated

        assert _preview(tmp_path, source)["follow_default_blocked"] is False
        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(source), "单3D", log_fn=lambda _m: None
        )
        assert result["ok"] is True, result

    def test_unresolved_follow_default_still_blocks(self, tmp_path: Path) -> None:
        source = _model(tmp_path, "L36程序")
        borrower = _model(tmp_path, "L37程序")
        _write_blocks(source, [("默认", {}), ("旧块", {"主板": "甲"})])
        self._borrower_follow_default(tmp_path, source, borrower)

        migrate_follow_default_refs(str(tmp_path), str(tmp_path), log_fn=lambda _m: None)

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(source), "单3D", log_fn=lambda _m: None
        )
        assert result["code"] == "follow_default_migration_required"

    def test_blocking_issue_returns_reference_incomplete(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        other = _model(tmp_path, "L37程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"主板": "甲"})])
        (other / "型号配置.toml").write_text("model_id = [broken\n", encoding="utf-8")
        before = _config(model).read_bytes()

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["code"] == "reference_incomplete"
        assert _config(model).read_bytes() == before

    def test_preview_rejects_on_blocking_issue(self, tmp_path: Path) -> None:
        """4.2 步骤 2：阻断级 issue 下预览**不发布可执行内容**。"""
        model = _model(tmp_path, "L36程序")
        other = _model(tmp_path, "L37程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"主板": "甲"})])
        (other / "型号配置.toml").write_text("model_id = [broken\n", encoding="utf-8")

        result = preview_platform_normalize(str(tmp_path), str(tmp_path), str(model))

        assert result["ok"] is False
        assert result["code"] == "reference_incomplete"
        assert "preview" not in result["payload"]

    def test_residual_blocks_even_when_already_normalized(self, tmp_path: Path) -> None:
        """已归一 + 同名，但仍有入向残留 → 必须阻断，不得返回 unchanged。"""
        source = _model(tmp_path, "L36程序")
        borrower = _model(tmp_path, "L37程序")
        _write_blocks(source, [("单3D", {"语音板": "程序A"})])
        self._borrower_follow_default(tmp_path, source, borrower)

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(source), "单3D", log_fn=lambda _m: None
        )

        assert result["ok"] is False
        assert result["code"] == "follow_default_migration_required"

    def test_residual_blocks_before_chassis_type_locked(self, tmp_path: Path) -> None:
        source = _model(tmp_path, "L36程序")
        borrower = _model(tmp_path, "L37程序")
        _write_blocks(source, [("单3D", {"语音板": "程序A"})])
        self._borrower_follow_default(tmp_path, source, borrower)
        before = _config(source).read_bytes()

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(source), "双2D", log_fn=lambda _m: None
        )

        assert result["code"] == "follow_default_migration_required"
        assert _config(source).read_bytes() == before

    def test_migrate_wrapper_is_pass_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """薄封装必须逐字段直通：参数原样转发，返回对象**同一性**相等。"""
        sentinel: ServiceResult = {
            "ok": True,
            "code": "ok",
            "message": "哨兵",
            "payload": {"changed": ["x"], "converted": 1},
        }
        seen: list[tuple[Any, ...]] = []

        def _fake(
            configured_root: Any, workspace_root: Any, *, log_fn: Any
        ) -> ServiceResult:
            seen.append((configured_root, workspace_root, log_fn))
            return sentinel

        monkeypatch.setattr(pns, "migrate_follow_default_refs", _fake)
        logger = lambda _m: None  # noqa: E731

        result = migrate_follow_defaults_for_normalize(
            str(tmp_path), str(tmp_path / "ws"), log_fn=logger
        )

        assert result is sentinel
        assert seen == [(str(tmp_path), str(tmp_path / "ws"), logger)]

    def test_migrate_wrapper_matches_underlying_on_real_workspace(
        self, tmp_path: Path
    ) -> None:
        """真实工作区上：封装跑完后，底层再跑一次应是幂等的 ok（无额外写入）。"""
        source = _model(tmp_path, "L36程序")
        borrower = _model(tmp_path, "L37程序")
        _write_blocks(source, [("默认", {"语音板": "程序A"})])
        self._borrower_follow_default(tmp_path, source, borrower)

        wrapped = migrate_follow_defaults_for_normalize(
            str(tmp_path), str(tmp_path), log_fn=lambda _m: None
        )
        assert wrapped["ok"] is True, wrapped
        assert wrapped["payload"]["converted"] == 1

        again = migrate_follow_default_refs(
            str(tmp_path), str(tmp_path), log_fn=lambda _m: None
        )
        assert again["ok"] is True
        assert again["code"] == wrapped["code"]
        assert set(again["payload"]) == set(wrapped["payload"])
        assert again["payload"]["converted"] == 0


class TestNormalizeExecution:
    def test_success_writes_single_canonical_block(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(
            model, [("默认", {"机芯版": "甲"}), ("标准单机芯3D", {"主板": "乙"})]
        )

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "双2D", log_fn=lambda _m: None
        )

        assert result["ok"] is True, result
        platforms, status, _ = load_platform_config_with_status(model)
        assert status == "ok"
        assert len(platforms) == 1
        assert platforms[0].platform_name == "双2D"
        assert platforms[0].defaults == {"机芯板": "甲", "主板": "乙"}
        _assert_clean(tmp_path)

    def test_module_choice_flow(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"主板": "乙"})])

        missing = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )
        assert missing["code"] == "module_choice_required"

        bad = normalize_platform_config(
            str(tmp_path),
            str(tmp_path),
            str(model),
            "单3D",
            {"主板": "丙"},
            log_fn=lambda _m: None,
        )
        assert bad["code"] == "invalid_choice"

        extra = normalize_platform_config(
            str(tmp_path),
            str(tmp_path),
            str(model),
            "单3D",
            {"主板": "甲", "语音板": "丁"},
            log_fn=lambda _m: None,
        )
        assert extra["code"] == "invalid_args"

        ok = normalize_platform_config(
            str(tmp_path),
            str(tmp_path),
            str(model),
            "单3D",
            {"主板": "乙"},
            log_fn=lambda _m: None,
        )
        assert ok["ok"] is True, ok
        platforms, _s, _e = load_platform_config_with_status(model)
        assert platforms[0].defaults == {"主板": "乙"}

    def test_unique_key_in_choices_is_invalid_args(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"语音板": "乙"})])

        result = normalize_platform_config(
            str(tmp_path),
            str(tmp_path),
            str(model),
            "单3D",
            {"主板": "甲"},
            log_fn=lambda _m: None,
        )

        assert result["code"] == "invalid_args"

    def test_invalid_chassis_type(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"})])

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "三3D", log_fn=lambda _m: None
        )

        assert result["code"] == "invalid_chassis_type"

    def test_already_normalized_same_name_is_unchanged(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"主板": "甲"})])
        before = _config(model).read_bytes()
        generation = load_workspace_status(tmp_path).generation

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["ok"] is True
        assert result["code"] == "unchanged"
        assert _config(model).read_bytes() == before
        assert load_workspace_status(tmp_path).generation == generation

    def test_chassis_type_locked_when_normalized(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"主板": "甲"})])
        before = _config(model).read_bytes()

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "双2D", log_fn=lambda _m: None
        )

        assert result["code"] == "chassis_type_locked"
        assert _config(model).read_bytes() == before

    def test_chassis_type_locked_with_alias_duplicate(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"机芯版": "甲", "机芯板": "甲"})])
        before = _config(model).read_bytes()

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "双2D", log_fn=lambda _m: None
        )

        assert result["code"] == "chassis_type_locked"
        assert _config(model).read_bytes() == before

    def test_chassis_type_locked_with_value_conflict(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"机芯版": "甲", "机芯板": "乙"})])
        before = _config(model).read_bytes()

        result = normalize_platform_config(
            str(tmp_path),
            str(tmp_path),
            str(model),
            "双2D",
            {"机芯板": "甲"},
            log_fn=lambda _m: None,
        )

        assert result["code"] == "chassis_type_locked"
        assert _config(model).read_bytes() == before

    def test_single_enum_block_alias_same_name_succeeds(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"机芯版": "甲", "机芯板": "甲"})])

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["ok"] is True, result
        assert result["code"] == "ok"
        platforms, _s, _e = load_platform_config_with_status(model)
        assert platforms[0].platform_name == "单3D"
        assert platforms[0].defaults == {"机芯板": "甲"}

    def test_multiple_legacy_blocks_not_locked(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"语音板": "乙"})])

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "上3D下2D", log_fn=lambda _m: None
        )

        assert result["ok"] is True, result

    def test_one_enum_block_among_many_not_locked(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"主板": "甲"}), ("默认", {"语音板": "乙"})])

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "双2D", log_fn=lambda _m: None
        )

        assert result["ok"] is True, result
        platforms, _s, _e = load_platform_config_with_status(model)
        assert platforms[0].platform_name == "双2D"

    def test_missing_config(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).unlink()

        preview = preview_platform_normalize(str(tmp_path), str(tmp_path), str(model))
        assert preview["code"] == "platform_config_missing"

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )
        assert result["code"] == "platform_config_missing"

    def test_zero_blocks(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text("# 空配置\n", encoding="utf-8")

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["code"] == "platform_config_missing"

    def test_parse_error_keeps_file(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text("[[platform]\nname =\n", encoding="utf-8")
        before = _config(model).read_bytes()

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["code"] == "config_parse_error"
        assert _config(model).read_bytes() == before

    def test_unnamed_block_is_rejected(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            "[[platform]]\n"
            'name = "默认"\n'
            "[platform.defaults]\n"
            '"主板" = "甲"\n'
            "\n"
            "[[platform]]\n"
            "[platform.defaults]\n"
            '"语音板" = "乙"\n',
            encoding="utf-8",
        )
        before = _config(model).read_bytes()

        preview = preview_platform_normalize(str(tmp_path), str(tmp_path), str(model))
        assert preview["code"] == "config_parse_error"

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["code"] == "config_parse_error"
        assert _config(model).read_bytes() == before
        assert "乙" in _config(model).read_text(encoding="utf-8")

    @pytest.mark.parametrize("literal", ["3", "true"])
    def test_non_string_name_rejected(self, tmp_path: Path, literal: str) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            f"[[platform]]\nname = {literal}\n[platform.defaults]\n\"主板\" = \"甲\"\n",
            encoding="utf-8",
        )
        before = _config(model).read_bytes()

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["code"] == "config_parse_error"
        assert "name" in result["payload"]["detail"]
        assert _config(model).read_bytes() == before

    @pytest.mark.parametrize("literal", ["3", "true", '["a", "b"]', "{ x = 1 }"])
    def test_non_string_defaults_value_rejected(self, tmp_path: Path, literal: str) -> None:
        model = _model(tmp_path, "L36程序")
        _config(model).write_text(
            f'[[platform]]\nname = "默认"\n[platform.defaults]\n"主板" = {literal}\n',
            encoding="utf-8",
        )
        before = _config(model).read_bytes()

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["code"] == "config_parse_error"
        assert "主板" in result["payload"]["detail"]
        assert _config(model).read_bytes() == before

    def test_read_source_matches_loader_for_string_config(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"语音板": "乙"})])

        _preimage, _data, platforms, status, _detail = pns._read_platform_source(model)
        expected, expected_status, _err = load_platform_config_with_status(model)

        assert status == expected_status
        assert [(p.platform_name, p.defaults) for p in platforms] == [
            (p.platform_name, p.defaults) for p in expected
        ]

    def test_cas_detects_write_after_preimage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"语音板": "乙"})])
        original = pns._cas_write

        def _sneaky(path: Path, preimage: bytes, content: str) -> bool:
            _config(model).write_bytes(preimage + "\n# 外部改动\n".encode())
            return original(path, preimage, content)

        monkeypatch.setattr(pns, "_cas_write", _sneaky)

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["code"] == "stale_plan"
        _assert_clean(tmp_path)

    def test_expected_detects_added_block(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"语音板": "乙"})])
        expectation = _preview(tmp_path, model)["expectation"]
        _write_blocks(
            model,
            [("默认", {"主板": "甲"}), ("旧块", {"语音板": "乙"}), ("新块", {})],
        )
        before = _config(model).read_bytes()

        result = normalize_platform_config(
            str(tmp_path),
            str(tmp_path),
            str(model),
            "单3D",
            expected=expectation,
            log_fn=lambda _m: None,
        )

        assert result["code"] == "stale_plan"
        assert _config(model).read_bytes() == before

    def test_expected_detects_changed_value(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"主板": "乙"})])
        expectation = _preview(tmp_path, model)["expectation"]
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"主板": "丙"})])
        before = _config(model).read_bytes()

        result = normalize_platform_config(
            str(tmp_path),
            str(tmp_path),
            str(model),
            "单3D",
            {"主板": "甲"},
            expected=expectation,
            log_fn=lambda _m: None,
        )

        assert result["code"] == "stale_plan"
        assert _config(model).read_bytes() == before

    def test_expected_none_skips_only_that_check(self, tmp_path: Path) -> None:
        source = _model(tmp_path, "L36程序")
        borrower = _model(tmp_path, "L37程序")
        _write_blocks(source, [("默认", {"语音板": "程序A"}), ("旧块", {"主板": "甲"})])
        TestFollowDefaultGate()._borrower_follow_default(tmp_path, source, borrower)

        result = normalize_platform_config(
            str(tmp_path),
            str(tmp_path),
            str(source),
            "单3D",
            expected=None,
            log_fn=lambda _m: None,
        )

        assert result["code"] == "follow_default_migration_required"

    def test_comment_only_change_after_preview_still_succeeds(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"语音板": "乙"})])
        expectation = _preview(tmp_path, model)["expectation"]
        _config(model).write_bytes(_config(model).read_bytes() + "\n# 后加注释\n".encode())

        result = normalize_platform_config(
            str(tmp_path),
            str(tmp_path),
            str(model),
            "单3D",
            expected=expectation,
            log_fn=lambda _m: None,
        )

        assert result["ok"] is True, result

    def test_chassis_type_changed_payload(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"}), ("旧块", {"语音板": "乙"})])

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["payload"]["chassis_type_changed"] is True
        assert result["payload"]["chassis_type"] == "单3D"

    def test_out_of_workspace_and_invalid_model_root(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _model(workspace, "L36程序")
        plain = workspace / "散落目录"
        plain.mkdir()

        out = normalize_platform_config(
            str(workspace), str(workspace), str(outside), "单3D", log_fn=lambda _m: None
        )
        assert out["code"] == "out_of_workspace"

        invalid = normalize_platform_config(
            str(workspace), str(workspace), str(plain), "单3D", log_fn=lambda _m: None
        )
        assert invalid["code"] == "invalid_model_root"

    def test_normalize_touches_only_platform_config(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _asset(model, "主板", "程序A")
        scheme = model / "定制" / "方案甲"
        scheme.mkdir(parents=True)
        (scheme / "方案配置.toml").write_text('platform = "标准单机芯3D"\n', encoding="utf-8")
        _write_blocks(model, [("默认", {"主板": "程序A"}), ("旧块", {"语音板": "乙"})])
        model_config_before = (model / "型号配置.toml").read_bytes()
        scheme_before = (scheme / "方案配置.toml").read_bytes()
        tree_before = sorted(
            str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir()
        )

        result = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )

        assert result["ok"] is True, result
        assert (model / "型号配置.toml").read_bytes() == model_config_before
        assert (scheme / "方案配置.toml").read_bytes() == scheme_before
        assert (
            sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir())
            == tree_before
        )


class TestLowLevelApiTightening:
    def test_set_default_variant_rejects_other_block_when_normalized(
        self, tmp_path: Path
    ) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"主板": "甲"})])
        before = _config(model).read_bytes()

        result = set_default_variant(
            str(model),
            "标准单机芯3D",
            "语音板",
            "乙",
            workspace_root=tmp_path,
            log_fn=lambda _m: None,
        )

        assert result["code"] == "platform_normalized_locked"
        assert _config(model).read_bytes() == before

    def test_set_default_variant_allows_same_block_when_normalized(
        self, tmp_path: Path
    ) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("单3D", {"主板": "甲"})])

        result = set_default_variant(
            str(model),
            "单3D",
            "语音板",
            "乙",
            workspace_root=tmp_path,
            log_fn=lambda _m: None,
        )

        assert result["ok"] is True, result
        platforms, _s, _e = load_platform_config_with_status(model)
        assert len(platforms) == 1
        assert platforms[0].defaults["语音板"] == "乙"

    def test_set_default_variant_unchanged_for_legacy_model(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(model, [("默认", {"主板": "甲"})])

        result = set_default_variant(
            str(model),
            "标准单机芯3D",
            "语音板",
            "乙",
            workspace_root=tmp_path,
            log_fn=lambda _m: None,
        )

        assert result["ok"] is True, result
        platforms, _s, _e = load_platform_config_with_status(model)
        assert len(platforms) == 2

    def test_ensure_platform_blocks_no_op_when_normalized(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        scheme = model / "定制" / "方案甲"
        scheme.mkdir(parents=True)
        (scheme / "方案配置.toml").write_text('platform = "标准单机芯3D"\n', encoding="utf-8")
        blocks = [PlatformDefaults("单3D", {"主板": "甲"})]

        result = ensure_platform_blocks(model, blocks)

        assert [p.platform_name for p in result] == ["单3D"]

    def test_ensure_platform_blocks_still_extends_legacy(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        scheme = model / "定制" / "方案甲"
        scheme.mkdir(parents=True)
        (scheme / "方案配置.toml").write_text('platform = "标准单机芯3D"\n', encoding="utf-8")
        blocks = [PlatformDefaults("默认", {"主板": "甲"})]

        result = ensure_platform_blocks(model, blocks)

        assert [p.platform_name for p in result] == ["默认", "标准单机芯3D"]


class TestD54Boundary:
    """9.5：第 5 节的低层收紧不得间接收紧 R8 / 6a 在多块型号上的既有路径。"""

    def _block_bytes(self, model_root: Path, block_index: int) -> bytes:
        """取某个 ``[[platform]]`` 块的原始字节片段（含其 defaults）。"""
        text = _config(model_root).read_text(encoding="utf-8")
        chunks = text.split("[[platform]]")
        return chunks[block_index + 1].encode("utf-8")

    def test_r8_rename_only_rewrites_matched_block(self, tmp_path: Path) -> None:
        from fwasset.core.services.reference_service import (
            apply_rewrite_plan,
            build_rewrite_plan,
        )
        from fwasset.core.types import RewriteRequest

        model = _model(tmp_path, "L36程序")
        old = _asset(model, "快捷键", "贝乐")
        _write_blocks(
            model,
            [
                ("标准单机芯3D", {"快捷键程序": "贝乐"}),
                ("双2D", {"快捷键程序": "量产_默认", "主板程序": "v1"}),
            ],
        )
        untouched_before = self._block_bytes(model, 1)

        plan = build_rewrite_plan(
            str(tmp_path),
            str(tmp_path),
            RewriteRequest(
                operation="rename",
                target_kind="asset",
                old_path=str(old),
                new_path=str(old.parent / "贝乐改"),
            ),
        )
        assert plan["ok"] is True, plan
        applied = apply_rewrite_plan(plan["payload"]["plan"], str(tmp_path))
        assert applied["ok"] is True, applied

        platforms, _s, _e = load_platform_config_with_status(model)
        assert platforms[0].defaults["快捷键程序"] == "贝乐改"
        assert self._block_bytes(model, 1) == untouched_before

    def test_multi_block_update_asset_keeps_other_blocks(self, tmp_path: Path) -> None:
        from fwasset.core.file_scan import scan_firmware_subtree
        from fwasset.core.services.layout_update_service import update_asset

        model = _model(tmp_path, "L36程序")
        old = _asset(model, "快捷键", "贝乐")
        _write_blocks(
            model,
            [
                ("标准单机芯3D", {"快捷键程序": "贝乐"}),
                ("双2D", {"快捷键程序": "量产_默认", "主板程序": "v1"}),
            ],
        )
        untouched_before = self._block_bytes(model, 1)
        source = tmp_path / "新程序"
        source.mkdir()
        (source / "key.hex").write_text("new", encoding="utf-8")
        assets, issues = scan_firmware_subtree(str(tmp_path), str(old))
        assert not issues and len(assets) == 1

        result = update_asset(
            str(tmp_path),
            str(tmp_path),
            assets[0],
            source,
            retire_mode="retire_to_backup",
            log_fn=lambda *_a, **_k: None,
        )

        assert result["ok"] is True, result
        assert self._block_bytes(model, 1) == untouched_before

    def test_multi_block_update_vendor_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fwasset.core.file_scan import scan_firmware_subtree
        from fwasset.core.services import write_edit_service
        from fwasset.core.services.write_edit_service import update_asset_vendor

        # 索引是搜索缓存，本用例只关心平台配置不被波及（与 5a 同款处理）
        monkeypatch.setattr(write_edit_service, "replace_asset", lambda *a, **k: None)
        model = _model(tmp_path, "L36程序")
        asset_root = _asset(model, "快捷键", "贝乐")
        _write_blocks(
            model,
            [("标准单机芯3D", {"快捷键程序": "贝乐"}), ("双2D", {"主板程序": "v1"})],
        )
        before = _config(model).read_bytes()
        assets, issues = scan_firmware_subtree(str(tmp_path), str(asset_root))
        assert not issues and len(assets) == 1

        result = update_asset_vendor(
            str(tmp_path), str(tmp_path), assets[0], "摩众", log_fn=lambda _m: None
        )

        assert result["ok"] is True, result
        assert _config(model).read_bytes() == before

    def test_set_default_variant_unaffected_for_multi_block(self, tmp_path: Path) -> None:
        model = _model(tmp_path, "L36程序")
        _write_blocks(
            model, [("标准单机芯3D", {"快捷键程序": "贝乐"}), ("双2D", {"主板程序": "v1"})]
        )

        result = set_default_variant(
            str(model),
            "双2D",
            "快捷键",
            "量产_默认",
            workspace_root=tmp_path,
            log_fn=lambda _m: None,
        )

        assert result["ok"] is True, result
        platforms, _s, _e = load_platform_config_with_status(model)
        assert len(platforms) == 2
        assert platforms[1].defaults["快捷键程序"] == "量产_默认"


class TestSetAssetDefaultHandoff:
    def test_normalize_unblocks_set_asset_default(self, tmp_path: Path) -> None:
        from fwasset.core.file_scan import scan_firmware_subtree
        from fwasset.core.services.write_edit_service import set_asset_default

        model = _model(tmp_path, "L36程序")
        asset_root = _asset(model, "主板", "程序A")
        _write_blocks(model, [("默认", {"主板": "程序A"}), ("旧块", {"主板": "程序A"})])
        assets, issues = scan_firmware_subtree(str(tmp_path), str(asset_root))
        assert not issues and len(assets) == 1

        before = set_asset_default(str(tmp_path), str(tmp_path), assets[0], log_fn=lambda _m: None)
        assert before["code"] == "platform_not_normalized"

        normalized = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )
        assert normalized["ok"] is True, normalized

        after = set_asset_default(str(tmp_path), str(tmp_path), assets[0], log_fn=lambda _m: None)
        assert after["ok"] is True, after

    def test_normalize_removes_canonical_duplicate(self, tmp_path: Path) -> None:
        from fwasset.core.file_scan import scan_firmware_subtree
        from fwasset.core.services.write_edit_service import set_asset_default

        model = _model(tmp_path, "L36程序")
        asset_root = _asset(model, "快捷键", "程序A")
        _write_blocks(model, [("单3D", {"快捷键": "程序A", "快捷按键": "程序A"})])
        assets, issues = scan_firmware_subtree(str(tmp_path), str(asset_root))
        assert not issues and len(assets) == 1

        before = set_asset_default(str(tmp_path), str(tmp_path), assets[0], log_fn=lambda _m: None)
        assert before["code"] == "canonical_duplicate"

        normalized = normalize_platform_config(
            str(tmp_path), str(tmp_path), str(model), "单3D", log_fn=lambda _m: None
        )
        assert normalized["ok"] is True, normalized

        after = set_asset_default(str(tmp_path), str(tmp_path), assets[0], log_fn=lambda _m: None)
        assert after["ok"] is True, after


def test_normalize_expectation_typed_dict_shape() -> None:
    expectation: NormalizeExpectation = {"block_names": [], "module_values": {}}
    assert expectation["block_names"] == []
