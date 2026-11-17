import test from "node:test";
import assert from "node:assert/strict";
import { knowledgeRows } from "../../src/g15/console/static/knowledge.js";
import { maintenanceCatalog } from "../../src/g15/console/static/maintenance-catalog.js";
test("readonly knowledge page covers all canonical rules and provenance", () => {
  const rows = knowledgeRows();
  assert.equal(rows.length, 8);
  assert.deepEqual(rows.map(r => r.id), maintenanceCatalog.entries.map(r => r.id));
  for (const row of rows) {
    assert.ok(row.condition.includes(row.feature));
    assert.ok(row.condition.includes("风险值大于 0"));
    assert.ok(row.action && row.basis && row.source && row.scope);
    assert.equal(row.version, maintenanceCatalog.version);
    assert.equal(row.sourceId, maintenanceCatalog.source_id);
  }
});


test("requested visual copy is removed without changing canonical provenance", async () => {
  const { readFile } = await import("node:fs/promises");
  const { maintenanceSourceLabel } = await import("../../src/g15/console/static/state.js");
  const html = await readFile(new URL("../../src/g15/console/static/knowledge.html", import.meta.url), "utf8");
  assert.ok(!html.includes('<section class="notice">'));
  assert.ok(maintenanceCatalog.source.includes("课程示例，未经设备厂商验证"));
  assert.equal(maintenanceSourceLabel(maintenanceCatalog.source), "G15 冻结版维护检查映射（936c127）");
  assert.equal(maintenanceSourceLabel("other source"), "other source");
  assert.equal(maintenanceSourceLabel(null), "");
});
