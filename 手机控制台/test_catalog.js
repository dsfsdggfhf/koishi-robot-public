"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = __dirname;
const config = JSON.parse(fs.readFileSync(path.join(app, "..", "config.json"), "utf8"));
const context = {window:{}};
vm.runInNewContext(fs.readFileSync(path.join(app, "catalog.js"), "utf8"), context);
const groups = context.window.KOISHI_CATALOG;
const switches = [];
function walk(value, prefix = "") {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  if (prefix && typeof value.enabled === "boolean") switches.push(prefix);
  for (const [key, child] of Object.entries(value)) if (child && typeof child === "object") walk(child, prefix ? `${prefix}.${key}` : key);
}
walk(config);
const described = groups.flatMap(group => group.paths);
assert.equal(groups.length, 40, "分组数量应为 40");
assert.equal(described.length, switches.length, "功能开关数量不一致");
assert.equal(new Set(described).size, described.length, "存在重复功能开关");
assert.deepEqual([...described].sort(), [...switches].sort(), "功能开关有遗漏或路径错误");
const pages = new Set([...fs.readFileSync(path.join(app, "index.html"), "utf8").matchAll(/id="features-([^"]+)"/g)].map(match => match[1]));
for (const group of groups) {
  assert.ok(pages.has(group.page), `不存在的页面：${group.page}`);
  assert.ok(group.title && group.summary && group.detail, `分组缺少说明：${group.page}`);
}
const script = fs.readFileSync(path.join(app, "app.js"), "utf8");
for (const group of groups) assert.ok(script.includes(`"${group.page}"`), `脚本缺少页面：${group.page}`);
const pageImages = [...script.matchAll(/art:"(\/koishi-[^\"]+\.png)"/g)].map(match => match[1]);
assert.equal(pageImages.length, 16, "应有 16 个一级页面立绘");
assert.equal(new Set(pageImages).size, 16, "一级页面立绘不能重复");
for (const image of pageImages) {
  assert.ok(fs.existsSync(path.join(app, image.slice(1))), `缺少页面立绘：${image}`);
}
const gallery = fs.readFileSync(path.join(app, "gallery.html"), "utf8");
for (const match of gallery.matchAll(/src="(koishi-[^\"]+\.png)"/g)) assert.ok(fs.existsSync(path.join(app, match[1])), `画廊图片不存在：${match[1]}`);
console.log(`通过：16 个页面、${groups.length} 个说明分组、${switches.length} 个真实开关和立绘文件。`);
