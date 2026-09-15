"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");

const html = fs.readFileSync("specs/design/prototypes/firmware-crud-prototype.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const dataLayer = script.slice(
  script.indexOf("const ROOT"),
  script.indexOf("/* ============================================================\n   3. 工具"),
);
const createApi = new Function(`${dataLayer}\nreturn prototypeDataApi;`);
const api = createApi()();

const model = api.createModel("测试双2D型号", "双2D");
assert.equal(model.platform, "双2D");
assert.equal(api.createScheme(model.id, "测试方案").platform, "双2D");

const renamedModel = api.renameModel(model.id, "重命名后的型号");
assert.equal(renamedModel.name, "重命名后的型号");
const dupModel = api.createModel("重命名目标型号", "单2D");
assert.throws(() => api.renameModel(model.id, "重命名目标型号"), /同名型号/);
assert.equal(api.deleteModel(model.id).name, "重命名后的型号");
assert.throws(() => api.createScheme(model.id, "删除后方案"), /找不到型号/);

// 修订十三：程序列表为空，种子数据已清空（重新读取也不再带入演示程序）
assert.equal(api.asset("a01"), null);
assert.match(html, /let assets = \[\];\s*let borrows = \[\];/);
assert.match(html, /const SEED_ASSETS = \[\];/);
assert.match(html, /const SEED_BORROWS = \[\];/);
assert.doesNotMatch(html, /MODELS\.slice\(3\)/);
assert.match(html, /assets = structuredClone\(SEED_ASSETS\);\s*borrows = structuredClone\(SEED_BORROWS\);/);
assert.match(html, /function createAsset\(fields = \{\}\)/);
// 修订十四：设置只有程序文件夹 + 一个「维护与修复」按钮（重新读取与检查合并，说明文字移除）
assert.match(html, /id="maintain"/);
assert.doesNotMatch(html, /id="rescan"/);
assert.doesNotMatch(html, /id="verify"/);
assert.doesNotMatch(html, /软件从这个文件夹读取全部程序/);
assert.doesNotMatch(html, /平时用不到/);
// 修订十五：按钮改名「软件修复」（文字在按钮内、整行宽度），旧的「开始维护/维护与修复」说法移除
assert.match(html, /软件修复/);
assert.doesNotMatch(html, /开始维护/);
assert.doesNotMatch(html, /维护与修复/);
assert.match(html, /<b>添加文件<\/b>/);
assert.match(html, /<b>选择别的型号已有程序<\/b>/);
assert.doesNotMatch(html, /用别的型号已经有的程序/);
assert.doesNotMatch(html, /共用同一个程序/);
// 修订十七：程序类型枚举更新为 10 类
assert.match(html, /const TYPES = \["主板程序", "手控UI", "蓝牙程序", "语音程序", "快捷键程序", "腿部程序", "机芯板程序", "旋钮开关", "音波板", "其他资料"\];/);
assert.doesNotMatch(html, /电机程序/);
// 修订十八：预置定制方案清空，页面从无方案状态开始
assert.match(html, /let SCHEMES = \[\];/);
assert.doesNotMatch(html, /以色列-Royal-Z9/);
// 修订二十：预置型号清空，页面从零开始（无型号时主区与新增按钮不可用）
assert.match(html, /const MODELS = \[\];/);
assert.match(html, /model: "",/);
assert.doesNotMatch(html, /B12-共享按摩椅/);
assert.match(html, /点左上角「＋ 新建型号」/);
assert.match(html, /#btnAdd"\)\.disabled = !state\.model/);
// 修订二十一：新建型号去掉说明文字，「平台」统一改为「机芯类型」
assert.doesNotMatch(html, /新型号会先建立空的通用和定制位置/);
assert.doesNotMatch(html, /平台/);
assert.match(html, /for="modelPlatform">机芯类型<\/label>/);
assert.match(html, /const VENDORS = \["摩众", "国瑞", "亿微", "明锐"\];/);
assert.match(html, /function createVendor/);
assert.match(html, /id="addVendor"/);
assert.match(html, /function vendorOptions/);
assert.match(html, /for="newVendor">归属厂商<\/label>/);
assert.match(html, /for="updateVendor">归属厂商<\/label>/);
assert.doesNotMatch(html, /还不知道/);

const created = api.createAsset({ model: "l36", type: "主板程序", name: "测试程序", files: ["test.bin"] });
assert.equal(created.vendor, "摩众");
const vendorTagged = api.createAsset({ model: "l36", type: "主板程序", name: "国瑞主板", vendor: "国瑞", files: ["gr.bin"] });
assert.equal(vendorTagged.vendor, "国瑞");
assert.equal(api.updateAsset(created.id, { name: created.name, type: created.type, owner: created.owner, vendor: "明锐" }).vendor, "明锐");
assert.equal(created.files[0], "test.bin");
const before = api.asset(created.id);
const updated = api.updateAsset(created.id, {
  name: "更新后的主板程序",
  type: before.type,
  owner: before.owner,
  files: ["updated.bin"],
}, "keep");
assert.equal(updated.name, "更新后的主板程序");
assert.equal(api.backupsFor(created.id).length, 1);
assert.equal(api.backupsFor(created.id)[0].asset.name, "测试程序");

const restored = api.restoreBackup(created.id, api.backupsFor(created.id)[0].id);
assert.equal(restored.name, "测试程序");
assert.equal(api.backupsFor(created.id).length, 1);
assert.equal(api.backupsFor(created.id)[0].asset.name, "更新后的主板程序");

const followed = api.createAsset({ model: "l36", type: "蓝牙程序", name: "防夹功能", files: ["anti-pinch.bin"] });
const followedBefore = api.resolveBorrow({ mode: "follow_asset", fromAssetId: followed.id });
assert.equal(followedBefore.name, "防夹功能");
api.updateAsset(followed.id, {
  name: "防夹功能-新版",
  type: followedBefore.type,
  owner: followedBefore.owner,
  files: ["anti-pinch-new.bin"],
}, "discard");
assert.equal(api.resolveBorrow({ mode: "follow_asset", fromAssetId: followed.id }).name, "防夹功能-新版");

assert.equal(api.selectedBorrowId([{ id: created.id }, { id: followed.id }], followed.id, created.id), followed.id);
assert.doesNotMatch(html, /data-act="manageModels"/);
assert.match(html, /data-act="renameModel"/);
assert.match(html, /id="updateOwner"[\s\S]*value="__new__"/);
assert.match(html, /id="updateSchemeRow"/);
assert.match(html, /createScheme\(asset\.model, updateSchemeName/);
assert.doesNotMatch(html, /查看全部/);
assert.match(html, /查看备份/);
assert.match(html, /backup\.asset\.files\.map/);
assert.match(html, /data-act="update"[^>]*>更新程序/);
assert.match(html, /data-act="delete"[^>]*>删除程序/);
assert.doesNotMatch(html, /程序文件夹或压缩包/);
assert.doesNotMatch(html, /选择压缩包/);
assert.doesNotMatch(html, /选择文件夹…/);
assert.doesNotMatch(html, /pick-pop/);
assert.match(html, /zip/);
assert.match(html, /class="dropzone"/);
assert.match(html, /pickerHtml\("new"\)/);
assert.match(html, /pickerHtml\("update"\)/);
assert.match(html, /id="\$\{prefix\}Pick"/);
assert.match(html, /function inferFromPath\(pathOrName\)/);
assert.match(html, /function classifySource\(pathOrName\)/);
assert.match(html, /function sourceFromDrop/);
assert.match(html, /kind: "file"/);
assert.match(html, /拖到这里，或点选择/);
assert.match(html, /自动识别：/);
assert.match(html, /ins-danger-link/);
assert.match(html, /addDialog\("file", null, source\)/);
assert.match(html, /dataTransfer/);
// 修订九：介绍文件一并保存但不在界面展示；版本按 .rom 文件名解析
assert.match(html, /function splitScanned\(files\)/);
assert.match(html, /function simulateScan\(source\)/);
assert.match(html, /function romVersion\(programFiles\)/);
assert.match(html, /介绍文件会一起保存/);
assert.match(html, /extraFiles/);
assert.match(html, /厂商压缩包/);
assert.match(html, /含 \$\{a\.extraFiles\.length\} 个介绍文件/);
// 修订十一：类型由使用者自选，不做内容推导与严格校验
assert.doesNotMatch(html, /detectType/);
assert.doesNotMatch(html, /handControlCheck/);
assert.doesNotMatch(html, /newTypeLine/);
assert.match(html, /for="newType">程序类型<\/label>/);
assert.match(html, /for="updateType">程序类型<\/label>/);
assert.doesNotMatch(html, /程序类型请在下方选择/);
assert.doesNotMatch(html, /文件夹直接识别/);
assert.match(html, /手控 UI 需要同时有 \.rom 和 \.pkg 两个文件，版本按 \.rom 的文件名显示/);
assert.match(html, /类型由使用者自选/);
const modelContext = html.match(/function showModelContext\(event, modelId\) \{([\s\S]*?)\n\}/)?.[1] || "";
assert.doesNotMatch(modelContext, /openModelPop\(false\)/);
assert.match(html, /runModelAction\(button\.dataset\.act, modelContextId\);\s*modelContextId = null;\s*openModelPop\(false\);/);
// 修订十二：定制方案支持重命名与删除（右键菜单）
const schemeModel = api.createModel("方案操作测试型号", "单2D");
const schemeA = api.createScheme(schemeModel.id, "待改名方案");
assert.equal(api.renameScheme(schemeA.id, "改名后方案").name, "改名后方案");
assert.throws(() => api.renameScheme(schemeA.id, ""), /方案名称不能为空/);
const schemeB = api.createScheme(schemeModel.id, "另一个方案");
assert.throws(() => api.renameScheme(schemeA.id, "另一个方案"), /已有同名方案/);
assert.equal(api.deleteScheme(schemeB.id).name, "另一个方案");
assert.equal(api.createVendor("新合作厂商"), "新合作厂商");
assert.throws(() => api.createVendor("摩众"), /已有同名厂商/);
assert.throws(() => api.createVendor("  "), /厂商名称不能为空/);
assert.equal(api.deleteScheme(schemeA.id).name, "改名后方案");
assert.throws(() => api.deleteScheme(schemeA.id), /找不到方案/);
assert.match(html, /data-scheme="\$\{s\.id\}"/);
assert.match(html, /function showSchemeContext\(event, schemeId\)/);
assert.match(html, /function runSchemeAction\(action, schemeId\)/);
assert.match(html, /data-act="renameScheme"/);
assert.match(html, /data-act="deleteScheme"/);
assert.match(html, /runSchemeAction\(button\.dataset\.act, schemeContextId\)/);
assert.match(html, /#navScopes"\)\.addEventListener\("contextmenu"/);
assert.match(html, /function renameSchemeDialog\(scheme\)/);
assert.match(html, /function deleteSchemeDialog\(scheme\)/);
assert.match(html, /它下面的程序位置已同步更新/);
assert.match(html, /方案里有 \$\{count\} 个程序，会一起放进回收站/);

function parseApplyArgs(source, openParen) {
  const args = [];
  let start = openParen + 1;
  let depth = 0;
  for (let index = start; index < source.length; index += 1) {
    const char = source[index];
    const next = source[index + 1];
    if (char === "'" || char === '"' || char === "`") {
      const quote = char;
      index += 1;
      while (index < source.length) {
        if (source[index] === "\\") { index += 2; continue; }
        if (source[index] === quote) break;
        index += 1;
      }
      continue;
    }
    if (char === "/" && next === "/") {
      index = source.indexOf("\n", index + 2);
      if (index === -1) break;
      continue;
    }
    if (char === "/" && next === "*") {
      index = source.indexOf("*/", index + 2);
      if (index === -1) break;
      index += 1;
      continue;
    }
    if (char === "(" || char === "[" || char === "{") { depth += 1; continue; }
    if (char === ")" || char === "]" || char === "}") {
      if (char === ")" && depth === 0) {
        args.push(source.slice(start, index).trim());
        return args;
      }
      depth -= 1;
      continue;
    }
    if (char === "," && depth === 0) {
      args.push(source.slice(start, index).trim());
      start = index + 1;
    }
  }
  throw new Error("apply 调用缺少匹配的右括号");
}

const applyMatches = [...script.matchAll(/\bapply\(/g)];
assert.equal(applyMatches.length, 16);
assert.match(script.slice(0, applyMatches[0].index), /function\s+$/);
const applyCalls = applyMatches.slice(1).map(match => parseApplyArgs(script, match.index + "apply".length));
assert.equal(applyCalls.length, 15);
const expectedUndoableByMessage = new Map([
  ["`已改名为「${next}」`", false],
  ["`「${a.name}」已设为本型号默认`", false],
  ["`已新建型号「${modelNameInput.trim()}」`", false],
  ["`型号已重命名为「${value}」`", false],
  ["`型号「${model.name}」已删除`", true],
  ["`已新建方案「${schemeNameInput.trim()}」`", false],
  ["`方案已重命名为「${value}」`", false],
  ["`方案「${scheme.name}」已放进回收站`", true],
  ["`「${name}」已加进程序库`", false],
  ["`${type}现在用 ${modelName(fromModel)} 的程序`", false],
  ["`「${changes.name}」已更新`", false],
  ['"已恢复备用副本"', false],
  ['"已删除备用副本"', true],
  ["`${b.type}不再用 ${modelName(b.fromModel)} 的程序`", true],
  ["`「${a.name}」已放进回收站`", true],
]);
const actualUndoableByMessage = new Map();
for (const args of applyCalls) {
  assert.ok(args.length >= 2 && args.length <= 4, `apply 实参数量应为 2–4，实际为 ${args.length}`);
  assert.ok(!actualUndoableByMessage.has(args[1]), `提示文案必须唯一：${args[1]}`);
  actualUndoableByMessage.set(args[1], args[3] === "true");
  if (args.length === 4) assert.equal(args[3], "true");
}
assert.deepEqual(actualUndoableByMessage, expectedUndoableByMessage);
assert.equal([...actualUndoableByMessage.values()].filter(Boolean).length, 5);
assert.match(script, /function apply\(mutate, message, detail = "", undoable = false\)/);
assert.match(script, /const snap = undoable \? snapshot\(\) : null;/);
assert.match(script, /function removedSince\(snap\)/);
assert.match(script, /const record = snap \? archiveDeleted\(message, removedSince\(snap\)\) : null;/);
assert.match(script, /record \? \(\) => undoRecycleRecord\(record.id\) : null/);
assert.match(script, /undo \? 5000 : 3000/);
new Function(script);

function makeUndoEnvironment() {
  const sectionA = script.slice(script.indexOf("const snapshot = () => ({"), script.indexOf("/* ============================================================\n   5. 渲染"));
  const sectionB = script.slice(script.indexOf("let undoLast = null;"), script.indexOf("/* ============================================================\n   7. 模态"));
  const appended = [];
  const timers = [];
  let cloneCount = 0;
  let recycled = null;
  const createElement = () => ({
    listeners: {}, children: [], className: "", innerHTML: "", textContent: "", type: "",
    append(...children) { this.children.push(...children); },
    remove() { this.removed = true; },
    addEventListener(type, listener) { (this.listeners[type] ??= []).push(listener); },
  });
  const state = { selectedId: "before", renaming: true };
  const assets = [{ id: "before" }];
  const borrows = [];
  const backups = [];
  const models = [{ id: "model-before" }];
  const schemes = [];
  const createApi = new Function(
    "render", "esc", "$", "document", "structuredClone", "setTimeout", "state", "assets", "borrows", "backups", "MODELS", "SCHEMES", "RECYCLE_COLLECTIONS", "recycleCollection", "archiveDeleted", "restoreRecycleRecord",
    `${sectionA}\n${sectionB}\nreturn { apply, getAssets: () => assets, getUndoLast: () => undoLast };`,
  );
  const api = createApi(
    () => {}, String, selector => {
      assert.equal(selector, "#toasts");
      return { append: node => appended.push(node) };
    },
    { createElement }, value => {
      cloneCount += 1;
      return JSON.parse(JSON.stringify(value));
    },
    (fn, ms) => (timers.push({ fn, ms }), timers.length),
    state, assets, borrows, backups, models, schemes,
    ["assets", "borrows", "backups", "models", "schemes"],
    collection => ({ assets, borrows, backups, models, schemes }[collection]),
    (message, removed) => { recycled = removed; return { id: "undo-record" }; },
    () => { assets.push(...recycled.assets); return { ok: true }; },
  );
  return { api, appended, assets, cloneCount: () => cloneCount, timers };
}

const immediate = makeUndoEnvironment();
immediate.api.apply(() => { immediate.assets.push({ id: "added" }); }, "立即生效");
assert.equal(immediate.cloneCount(), 0);
assert.equal(immediate.timers[0].ms, 3000);
assert.equal(immediate.appended[0].children.length, 0);
assert.equal(immediate.api.getUndoLast(), null);

const undoable = makeUndoEnvironment();
const assetsBeforeUndo = JSON.parse(JSON.stringify(undoable.api.getAssets()));
undoable.api.apply(() => { undoable.assets.splice(0, 1); }, "删除程序", "", true);
assert.equal(undoable.cloneCount(), 5);
assert.equal(undoable.timers[0].ms, 5000);
const undoToast = undoable.appended[0];
const undoButton = undoToast.children[0];
assert.equal(undoToast.children.length, 1);
assert.notEqual(undoButton, undoToast);
assert.equal(undoButton.textContent, "撤销");
undoButton.listeners.click[0]();
assert.deepEqual(undoable.api.getAssets(), assetsBeforeUndo);
assert.equal(undoable.api.getUndoLast(), null);
assert.equal(undoable.appended.length, 2);
assert.match(undoable.appended[1].innerHTML, /已撤销上一步操作/);
const vendorManagement = script.slice(script.indexOf("function settingsDialog()"), script.indexOf("/* ============================================================\n   10. 事件"));
assert.doesNotMatch(vendorManagement, /\bapply\(/);

// 修订二十二：原型内置回收站 —— 默认保留 24 小时、可还原或彻底删除、到期移至 Windows 回收站
assert.match(html, /const RECYCLE_RETENTION_MS = 24 \* 60 \* 60 \* 1000;/);
assert.match(html, /openRecycleBin/);
assert.match(html, /<div class="top-actions">\s*<button class="button" id="openRecycleBin"[\s\S]*?<button class="button" id="btnSettings"[\s\S]*?<\/div>/);
assert.match(html, /\.top-actions \{ display: flex; align-items: center; gap: 8px; flex: 0 0 auto; \}/);
assert.match(html, /\.crumb \{ min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;/);
assert.match(html, /回收站（/);
assert.match(html, /已删除项目/);
assert.match(html, /还原/);
assert.match(html, /彻底删除/);
assert.match(html, /24 小时后会移至 Windows 回收站/);
assert.match(script, /function recycleBinDialog\(\)[\s\S]*?purgeExpiredRecycleBin\(\)/);
assert.match(script, /restoreRecycleRecord\(button.dataset.id\)/);
assert.match(script, /discardRecycleRecord\(button.dataset.id\)/);

// 修订二十三：回收站记录必须可在 24 小时内安全还原、彻底删除或到期清理。
const recycleApi = createApi()();
const recycleNow = 1_700_000_000_000;
const restoredAsset = { id: "recycle-asset", model: "model-r", name: "误删程序" };
const restorableRecord = recycleApi.archiveDeleted("误删程序", { assets: [restoredAsset] }, recycleNow);
assert.equal(restorableRecord.expiresAt, recycleNow + 24 * 60 * 60 * 1000);
assert.equal(recycleApi.recycleBinItems().length, 1);
assert.deepEqual(recycleApi.restoreRecycleRecord(restorableRecord.id, recycleNow), { ok: true });
assert.equal(recycleApi.asset(restoredAsset.id).name, "误删程序");
assert.equal(recycleApi.recycleBinItems().length, 0);

const conflictRecord = recycleApi.archiveDeleted("冲突程序", {
  assets: [{ id: restoredAsset.id, model: "model-r", name: "错误版本" }],
}, recycleNow + 1);
assert.deepEqual(recycleApi.restoreRecycleRecord(conflictRecord.id, recycleNow + 1), { ok: false, reason: "conflict" });
assert.equal(recycleApi.asset(restoredAsset.id).name, "误删程序");

const disposableRecord = recycleApi.archiveDeleted("临时副本", {
  backups: [{ id: "recycle-backup", asset: restoredAsset.id }],
}, recycleNow + 2);
assert.equal(recycleApi.discardRecycleRecord(disposableRecord.id), true);
assert.equal(recycleApi.recycleBinItems().some(item => item.id === disposableRecord.id), false);

const expiredRecord = recycleApi.archiveDeleted("过期副本", {
  backups: [{ id: "expired-backup", asset: restoredAsset.id }],
}, recycleNow - 24 * 60 * 60 * 1000 - 1);
assert.equal(recycleApi.purgeExpiredRecycleBin(recycleNow), 1);
assert.equal(recycleApi.recycleBinItems().some(item => item.id === expiredRecord.id), false);

console.log("prototype data interactions: 122 existing assertions and undo-scope assertions passed");
