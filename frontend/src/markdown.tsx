// Lightweight markdown -> React renderer (block + inline). Ported verbatim from
// the Claude Design component; presentation only.
import React from "react";

const MD: any = {
  p: { margin: "0 0 10px", lineHeight: 1.6 },
  list: { margin: "0 0 10px", paddingLeft: "22px" },
  li: { margin: "3px 0", lineHeight: 1.55 },
  pre: { background: "var(--color-neutral-200)", border: "1px solid var(--color-divider)", padding: "12px 14px", overflowX: "auto", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: "13px", lineHeight: 1.55, margin: "0 0 12px" },
  code: { background: "var(--color-neutral-200)", border: "1px solid var(--color-divider)", padding: "1px 5px", fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: "0.9em" },
  quote: { borderLeft: "3px solid var(--color-accent)", padding: "2px 0 2px 14px", margin: "0 0 10px", color: "color-mix(in srgb, var(--color-text) 75%, transparent)" },
  tableWrap: { overflowX: "auto", margin: "0 0 12px" },
};

function mdHeading(lvl: number): any {
  const size = ({ 1: 24, 2: 20, 3: 17, 4: 15, 5: 14, 6: 13 } as any)[lvl] || 15;
  return { fontFamily: "var(--font-heading)", fontWeight: 800, fontSize: size + "px", lineHeight: 1.2, letterSpacing: "-0.01em", margin: "14px 0 8px" };
}

function mdInline(text: string): any[] {
  const nodes: any[] = [];
  let rest = text || "";
  let buf = "";
  let k = 0;
  const pats = [
    { re: /^`([^`]+)`/, make: (m: any) => React.createElement("code", { key: k++, style: MD.code }, m[1]) },
    { re: /^\*\*([^*]+)\*\*/, make: (m: any) => React.createElement("strong", { key: k++ }, mdInline(m[1])) },
    { re: /^__([^_]+)__/, make: (m: any) => React.createElement("strong", { key: k++ }, mdInline(m[1])) },
    { re: /^\*([^*]+)\*/, make: (m: any) => React.createElement("em", { key: k++ }, mdInline(m[1])) },
    { re: /^\[([^\]]+)\]\(([^)\s]+)\)/, make: (m: any) => React.createElement("a", { key: k++, href: m[2], target: "_blank", rel: "noreferrer" }, m[1]) },
  ];
  while (rest.length) {
    let hit = false;
    for (const p of pats) {
      const m = p.re.exec(rest);
      if (m) {
        if (buf) { nodes.push(buf); buf = ""; }
        nodes.push(p.make(m));
        rest = rest.slice(m[0].length);
        hit = true;
        break;
      }
    }
    if (!hit) { buf += rest[0]; rest = rest.slice(1); }
  }
  if (buf) nodes.push(buf);
  return nodes;
}

function mdRow(line: string): string[] {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((s) => s.trim());
}

export function parseMarkdown(src: string): any[] {
  const lines = (src || "").replace(/\r\n/g, "\n").split("\n");
  const out: any[] = [];
  let i = 0;
  let k = 0;
  const isBlockStart = (l: string) => /^(#{1,6})\s|^\s*[-*+]\s|^\s*\d+\.\s|^```|^\s*>/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const code: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i++; }
      i++;
      out.push(React.createElement("pre", { key: k++, style: MD.pre }, React.createElement("code", null, code.join("\n"))));
      continue;
    }
    if (/^\s*$/.test(line)) { i++; continue; }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) { const lvl = h[1].length; out.push(React.createElement("h" + lvl, { key: k++, style: mdHeading(lvl) }, mdInline(h[2]))); i++; continue; }
    if (line.indexOf("|") !== -1 && i + 1 < lines.length && /^\s*\|?[\s:|-]*-[\s:|-]*$/.test(lines[i + 1]) && lines[i + 1].indexOf("|") !== -1) {
      const header = mdRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].indexOf("|") !== -1 && lines[i].trim()) { rows.push(mdRow(lines[i])); i++; }
      out.push(React.createElement("div", { key: k++, style: MD.tableWrap },
        React.createElement("table", { className: "table" },
          React.createElement("thead", null, React.createElement("tr", null, header.map((c, ci) => React.createElement("th", { key: ci }, mdInline(c))))),
          React.createElement("tbody", null, rows.map((r, ri) => React.createElement("tr", { key: ri }, r.map((c, ci) => React.createElement("td", { key: ci }, mdInline(c))))))
        )
      ));
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*[-*+]\s+/, "")); i++; }
      out.push(React.createElement("ul", { key: k++, style: MD.list }, items.map((it, ii) => React.createElement("li", { key: ii, style: MD.li }, mdInline(it)))));
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { items.push(lines[i].replace(/^\s*\d+\.\s+/, "")); i++; }
      out.push(React.createElement("ol", { key: k++, style: MD.list }, items.map((it, ii) => React.createElement("li", { key: ii, style: MD.li }, mdInline(it)))));
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const q: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, "")); i++; }
      out.push(React.createElement("blockquote", { key: k++, style: MD.quote }, mdInline(q.join(" "))));
      continue;
    }
    const para = [line];
    i++;
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !isBlockStart(lines[i])) { para.push(lines[i]); i++; }
    out.push(React.createElement("p", { key: k++, style: MD.p }, mdInline(para.join(" "))));
  }
  return out;
}
