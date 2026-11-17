import { maintenanceCatalog } from "./maintenance-catalog.js";

const names = { mean: "均值", std: "标准差", rms: "均方根", ptp: "峰峰值",
  kurtosis_excess: "超额峰度", dominant_freq_hz: "主频", crest_factor: "峰值因子",
  zero_crossing_rate: "过零率" };
export function knowledgeRows(catalog = maintenanceCatalog) {
  return catalog.entries.map((entry) => ({
    ...entry, label: names[entry.feature] || entry.feature,
    condition: `有效的黄色或红色告警，原因中含 ${entry.feature} 的有效测量值且风险值大于 0；证据或特征缺失不匹配。`,
    source: catalog.source, sourceId: catalog.source_id, version: catalog.version,
    scope: catalog.scope,
  }));
}
export function renderKnowledge(doc = document) {
  const make = (tag, value) => { const element = doc.createElement(tag); element.textContent = value; return element; };
  const rows = knowledgeRows();
  doc.getElementById("catalog-meta").textContent = `共 ${rows.length} 条规则 · 版本 ${maintenanceCatalog.version} · 来源 ${maintenanceCatalog.source_id} · ${maintenanceCatalog.scope}`;
  const cards = [], links = [];
  for (const row of rows) {
    const card = make("article", ""); card.id = row.id; card.tabIndex = -1;
    const heading = make("h2", `${row.id} · ${row.label}`);
    heading.append(make("span", row.feature)); card.append(heading);
    const detail = make("dl", "");
    for (const [label, value] of [["适用条件", row.condition], ["维护建议", row.action],
      ["判断依据", row.basis], ["规则来源", `${row.sourceId} · ${row.source}`],
      ["版本与范围", `${row.version} · ${row.scope}`]]) {
      detail.append(make("dt", label), make("dd", value));
    }
    card.append(detail); cards.push(card);
    const link = make("a", `${row.id} ${row.label}`); link.href = `#${row.id}`; links.push(link);
  }
  doc.getElementById("knowledge-list").replaceChildren(...cards);
  doc.getElementById("knowledge-index").replaceChildren(...links);
  doc.getElementById("knowledge-status").textContent = `已加载全部 ${rows.length} 条规则。点击条目编号可定位查看。`;
  const id = location.hash.slice(1);
  if (rows.some((row) => row.id === id)) doc.getElementById(id).scrollIntoView();
}
if (typeof document !== "undefined") renderKnowledge();
