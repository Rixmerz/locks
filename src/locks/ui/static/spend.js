import { get } from "./net.js";
import { cellChip, h, usd } from "./util.js";

const SERIES = ["opus", "sonnet", "haiku", "fable", "other", "workers"];

export async function renderSpend() {
  const s = await get("/api/spend?days=14");
  const top = Math.max(0.01, ...s.by_day.map((d) => SERIES.reduce((sum, k) => sum + (d[k] || 0), 0)));
  const bars = s.by_day.map((d) => {
    const bar = h("div", { class: "bar", title: `${d.day}: ${usd(SERIES.reduce((a, k) => a + (d[k] || 0), 0))}` },
      h("span", { class: "lbl" }, d.day.slice(5)));
    for (const k of SERIES) {
      if (!d[k]) continue;
      const seg = h("span", { class: `seg-${k}` });
      seg.style.height = `${(d[k] / top) * 130}px`;
      bar.append(seg);
    }
    return bar;
  });
  const workers = s.workers.reduce((a, w) => a + (w.usd || 0), 0);
  return h("div", {},
    h("div", { class: "stats" },
      h("div", { class: "stat" }, h("b", {}, usd(s.total_usd)), h("span", {}, "last 14 days, at API rates")),
      h("div", { class: "stat" }, h("b", {}, usd(Math.max(0, s.total_usd - workers))), h("span", {}, "sessions")),
      h("div", { class: "stat" }, h("b", {}, usd(workers)), h("span", {}, "workers"))),
    h("div", { class: "legend" }, SERIES.map((k) => h("span", {}, h("i", { class: `seg-${k}` }), k))),
    bars.length ? h("div", { class: "bars" }, bars) : h("div", { class: "empty" }, "no spend recorded yet"),
    h("h2", {}, "Sessions by model"),
    h("table", {}, h("tr", {}, h("th", {}, "model"), h("th", { class: "num" }, "messages"), h("th", { class: "num" }, "output"),
        h("th", { class: "num" }, "input + cache"), h("th", { class: "num" }, "cost")),
      s.sessions.map((r) => h("tr", {}, h("td", {}, r.model), h("td", { class: "num" }, r.messages),
        h("td", { class: "num" }, r.output_tokens.toLocaleString()),
        h("td", { class: "num" }, (r.input_tokens + r.cache_read + r.cache_write).toLocaleString()),
        h("td", { class: "num" }, usd(r.usd))))),
    h("h2", { class: "section" }, "Workers by cell"),
    h("table", {}, h("tr", {}, h("th", {}, "cell"), h("th", { class: "num" }, "attempts"), h("th", { class: "num" }, "passed"),
        h("th", { class: "num" }, "cost")),
      s.workers.map((w) => h("tr", {}, h("td", {}, cellChip(w.cell)), h("td", { class: "num" }, w.attempts),
        h("td", { class: "num" }, w.passed || 0), h("td", { class: "num" }, usd(w.usd))))));
}
