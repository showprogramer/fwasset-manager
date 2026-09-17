# Migration: 索引 schema v4 —— chassis_type 与 vendor 入库

Date: 2026-09-17  
Scope: `src/fwasset/core/asset_index.py`, `src/fwasset/core/file_scan.py`, `src/fwasset/core/asset_info.py`  
Status: Completed (SCHEMA_VERSION 3 → 4)

## Reason

机芯类型 `chassis_type`（D0.1）与厂商 `vendor`（D6）同批纳入索引，一次性提升 `schema_version`（D6.4）。目录树与 `程序信息.toml` 仍是真源，SQLite 只做搜索缓存——本次升级仅扩缓存列，不改数据源。

## Old → New

| 项 | 旧 | 新 |
|----|----|-----|
| `assets` 表列 | 无 `chassis_type`、`vendor` | 新增 `chassis_type`、`vendor` 两列（`TEXT NOT NULL DEFAULT ''`） |
| `_row_to_asset` | `chassis_type` 用 `"chassis_type" in keys` 临时特判 | 特判移除，直接读列 |
| `keyword_fields` / 专用索引 | — | `vendor` / `chassis_type` 不进 `keyword_fields`、不加专用索引（暂无查询消费，消费方随 UI 子任务 8 定） |
| v3 → v4 迁移分支 | — | 不加 ALTER 迁移：v3 及更早版本走既有「版本不兼容，请重新扫描生成」报错路径（不匹配即要求重扫）；既有 v1/v2 分支行为不变 |

## Impact

- v3 及更早索引库打开时报「版本不兼容，请重新扫描生成」，需全量重扫重建缓存。
- 固件目录、`程序信息.toml` 与用户配置不受影响。
- 扫描期 `程序信息.toml` 损坏仅产生 warning 级 ScanIssue、vendor 留空，不阻断索引对账。

## Verification

```bash
uv run python -m pytest src/fwasset/tests/test_asset_index.py src/fwasset/tests/test_vendor_metadata.py -q --no-cov
```

## Rollback

索引是缓存，直接删除 `fwasset.db` 重建即可，无需回滚表结构。
