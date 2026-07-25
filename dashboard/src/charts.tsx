// Data-driven SVG charts, ported from the Claude Design component. No chart
// library — plain React SVG so they re-render cleanly on refresh.
import React from "react";
import { nfmt, bucketLabel } from "./format";
import type { TimeseriesPoint } from "./api";

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / pow;
  const step = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
  return step * pow;
}

function axisText(x: number, y: number, str: string, anchor?: string) {
  return React.createElement(
    "text",
    { x, y, fill: "currentColor", opacity: 0.5, fontSize: 10, textAnchor: anchor || "middle", style: { fontFamily: "var(--font-body)" } },
    str
  );
}

function xLabels(points: TimeseriesPoint[], padL: number, iw: number, yBase: number) {
  if (!points.length) return [];
  const idxs =
    points.length <= 6 ? points.map((_, i) => i) : [0, Math.floor((points.length - 1) / 2), points.length - 1];
  return idxs.map((i) => axisText(padL + (iw / points.length) * (i + 0.5), yBase + 16, bucketLabel(points[i].start), "middle"));
}

export function buildBarChart(points: TimeseriesPoint[]) {
  const W = 620, H = 240, padL = 40, padR = 12, padT = 12, padB = 30;
  const iw = W - padL - padR, ih = H - padT - padB;
  const max = niceMax(Math.max(1, ...points.map((p) => p.calls)));
  const n = points.length || 1;
  const cw = iw / n, bw = Math.max(3, Math.min(38, cw - 6));
  const kids: any[] = [];
  for (let g = 0; g <= 4; g++) {
    const val = (max / 4) * g, y = padT + ih - (val / max) * ih;
    kids.push(React.createElement("line", { key: "g" + g, x1: padL, y1: y, x2: padL + iw, y2: y, stroke: "var(--color-divider)", strokeWidth: 1 }));
    kids.push(React.cloneElement(axisText(padL - 6, y + 3, nfmt(Math.round(val)), "end"), { key: "gy" + g }));
  }
  points.forEach((p, i) => {
    const cx = padL + cw * i + (cw - bw) / 2;
    const succ = Math.max(0, p.calls - p.errors);
    const hs = (succ / max) * ih, he = (p.errors / max) * ih;
    const yTop = padT + ih;
    if (hs > 0) kids.push(React.createElement("rect", { key: "s" + i, x: cx, y: yTop - hs, width: bw, height: hs, fill: "#0d9488" }));
    if (he > 0) kids.push(React.createElement("rect", { key: "e" + i, x: cx, y: yTop - hs - he, width: bw, height: he, fill: "#dc2626" }));
  });
  xLabels(points, padL, iw, padT + ih).forEach((t, i) => kids.push(React.cloneElement(t, { key: "x" + i })));
  kids.push(React.createElement("line", { key: "base", x1: padL, y1: padT + ih, x2: padL + iw, y2: padT + ih, stroke: "var(--color-text)", strokeWidth: 2, opacity: 0.85 }));
  return React.createElement(
    "svg",
    { viewBox: "0 0 " + W + " " + H, width: "100%", height: 240, style: { minWidth: 460, display: "block", color: "var(--color-text)" }, role: "img", "aria-label": "Throughput per time bucket, success and error calls stacked" },
    kids
  );
}

export function buildLineChart(points: TimeseriesPoint[]) {
  const W = 620, H = 240, padL = 44, padR = 12, padT = 12, padB = 30;
  const iw = W - padL - padR, ih = H - padT - padB;
  const lats = points.map((p) => p.avg_latency_ms).filter((v): v is number => v != null);
  const max = niceMax(Math.max(1, ...(lats.length ? lats : [1])));
  const n = points.length || 1;
  const kids: any[] = [];
  for (let g = 0; g <= 4; g++) {
    const val = (max / 4) * g, y = padT + ih - (val / max) * ih;
    kids.push(React.createElement("line", { key: "g" + g, x1: padL, y1: y, x2: padL + iw, y2: y, stroke: "var(--color-divider)", strokeWidth: 1 }));
    kids.push(React.cloneElement(axisText(padL - 6, y + 3, nfmt(Math.round(val)), "end"), { key: "gy" + g }));
  }
  const xAt = (i: number) => padL + (iw / n) * (i + 0.5);
  const yAt = (v: number) => padT + ih - (v / max) * ih;
  let d = "", started = false;
  points.forEach((p, i) => {
    if (p.avg_latency_ms == null) { started = false; return; }
    d += (started ? " L" : " M") + xAt(i) + " " + yAt(p.avg_latency_ms);
    started = true;
  });
  if (d) kids.push(React.createElement("path", { key: "line", d: d.trim(), fill: "none", stroke: "var(--color-accent)", strokeWidth: 2 }));
  points.forEach((p, i) => {
    if (p.avg_latency_ms != null) kids.push(React.createElement("rect", { key: "d" + i, x: xAt(i) - 2.5, y: yAt(p.avg_latency_ms) - 2.5, width: 5, height: 5, fill: "var(--color-accent)" }));
  });
  xLabels(points, padL, iw, padT + ih).forEach((t, i) => kids.push(React.cloneElement(t, { key: "x" + i })));
  kids.push(React.createElement("line", { key: "base", x1: padL, y1: padT + ih, x2: padL + iw, y2: padT + ih, stroke: "var(--color-text)", strokeWidth: 2, opacity: 0.85 }));
  return React.createElement(
    "svg",
    { viewBox: "0 0 " + W + " " + H, width: "100%", height: 240, style: { minWidth: 460, display: "block", color: "var(--color-text)" }, role: "img", "aria-label": "Average latency in milliseconds per time bucket" },
    kids
  );
}
