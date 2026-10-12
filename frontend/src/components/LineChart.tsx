// SVG line/area chart: tokens for colour, crosshair tooltip, one y-axis,
// optional percentile bands and a vertical marker. Every chart has a table alternative.
import { useEffect, useMemo, useRef, useState } from "react";
import { compact, date as fmtDate } from "../lib/format";

export type Series = { name: string; values: number[]; color: string; area?: boolean; dashed?: boolean };
export type Band = { lo: number[]; hi: number[]; color: string; opacity: number; label?: string };

type Props = {
  x: (string | number)[];
  series: Series[];
  bands?: Band[];
  height?: number;
  format?: (v: number) => string;
  xFormat?: (v: string | number) => string;
  marker?: { index: number; label: string };
  yMax?: number;
  label: string;
};

function niceTicks(lo: number, hi: number, count: number): number[] {
  const span = hi - lo || 1;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => s >= step0) ?? mag * 10;
  const out: number[] = [];
  for (let t = Math.ceil(lo / step) * step; t <= hi + 1e-9; t += step) out.push(t);
  return out;
}

export function useCssVar(name: string): string {
  const [v, setV] = useState(() => getComputedStyle(document.documentElement).getPropertyValue(name).trim());
  useEffect(() => {
    const upd = () => setV(getComputedStyle(document.documentElement).getPropertyValue(name).trim());
    window.addEventListener("pm:theme", upd);
    const mq = matchMedia("(prefers-color-scheme: light)");
    mq.addEventListener("change", upd);
    return () => {
      window.removeEventListener("pm:theme", upd);
      mq.removeEventListener("change", upd);
    };
  }, [name]);
  return v;
}

export function LineChart({ x, series, bands, height = 220, format = compact, xFormat, marker, yMax, label }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(600);
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const clip = useMemo(() => "c" + Math.random().toString(36).slice(2, 8), []);
  const empty = x.length === 0;
  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(Math.max(280, el.clientWidth)));
    ro.observe(el);
    return () => ro.disconnect();
  }, [empty]);
  const xf = xFormat ?? ((v: string | number) => (typeof v === "string" ? fmtDate(v) : String(v)));
  const n = x.length;
  const m = { t: 10, r: 56, b: 22, l: 8 };
  const geo = useMemo(() => {
    const all = series.flatMap((s) => s.values).concat(...(bands ?? []).flatMap((b) => [b.lo, b.hi]));
    const finite = all.filter(Number.isFinite);
    let lo = finite.length ? Math.min(...finite) : 0;
    let hi = finite.length ? Math.max(...finite) : 1;
    if (lo > 0 && lo < hi * 0.35) lo = 0;
    const pad = (hi - lo) * 0.08 || 1;
    if (lo !== 0) lo -= pad;
    hi += pad;
    if (yMax != null) hi = yMax;
    const X = (i: number) => m.l + (W - m.l - m.r) * (n <= 1 ? 0 : i / (n - 1));
    const Y = (v: number) => m.t + (height - m.t - m.b) * (1 - (v - lo) / (hi - lo || 1));
    return { lo, hi, X, Y, ticks: niceTicks(lo, hi, 4) };
  }, [series, bands, W, height, n, yMax, m.l, m.r, m.t, m.b]);
  if (n === 0)
    return (
      <div className="chart" ref={wrap} style={{ height, display: "grid", placeItems: "center", color: "var(--muted)" }}>
        No data for this period
      </div>
    );
  const { X, Y, ticks, lo } = geo;
  const xt = W < 480 ? [0, n - 1] : [0, Math.floor((n - 1) / 3), Math.floor((2 * (n - 1)) / 3), n - 1];
  const base = Y(Math.max(lo, 0));
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) * W) / r.width;
    setHover(Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / (W - m.l - m.r)) * (n - 1)))));
  };
  return (
    <div className="chart" ref={wrap}>
      {table ? (
        <div className="tbl-wrap" style={{ maxHeight: height, overflowY: "auto" }}>
          <table>
            <thead>
              <tr>
                <th>Date</th>
                {series.map((s) => (
                  <th key={s.name} className="r">{s.name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {x.map((xv, i) => (
                <tr key={i} style={{ cursor: "default" }}>
                  <td className="num">{xf(xv)}</td>
                  {series.map((s) => (
                    <td key={s.name} className="r num">{format(s.values[i] ?? NaN)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <svg viewBox={`0 0 ${W} ${height}`} height={height} role="img" aria-label={label} onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
          <defs>
            <clipPath id={clip}>
              <rect x={m.l} y={m.t - 6} width={W - m.l - m.r + 6} height={height - m.t - m.b + 6} />
            </clipPath>
          </defs>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={m.l} x2={W - m.r} y1={Y(t)} y2={Y(t)} stroke="var(--line)" strokeWidth={1} />
              <text x={W - m.r + 8} y={Y(t) + 3.5}>{format(t)}</text>
            </g>
          ))}
          {xt.map((i, k) => (
            <text key={k} x={X(i)} y={height - 6} textAnchor={k === 0 ? "start" : k === xt.length - 1 ? "end" : "middle"}>
              {xf(x[i]!)}
            </text>
          ))}
          <g clipPath={`url(#${clip})`}>
            {bands?.map((b, bi) => (
              <polygon
                key={bi}
                points={b.hi.map((v, i) => `${X(i)},${Y(v)}`).join(" ") + " " + b.lo.map((v, i) => `${X(i)},${Y(v)}`).reverse().join(" ")}
                fill={b.color}
                fillOpacity={b.opacity}
              />
            ))}
            {series.map((s) => {
              const pts = s.values.map((v, i) => `${X(i)},${Y(v)}`).join(" ");
              return (
                <g key={s.name}>
                  {s.area && <polygon points={`${X(0)},${base} ${pts} ${X(n - 1)},${base}`} fill={s.color} fillOpacity={0.1} />}
                  <polyline points={pts} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" strokeDasharray={s.dashed ? "4 4" : undefined} />
                  <circle cx={X(n - 1)} cy={Y(s.values[n - 1] ?? 0)} r={4} fill={s.color} stroke="var(--surface)" strokeWidth={2} />
                </g>
              );
            })}
          </g>
          {marker && (
            <g>
              <line x1={X(marker.index)} x2={X(marker.index)} y1={m.t} y2={height - m.b} stroke="var(--fg-2)" strokeWidth={1} />
              <text x={X(marker.index) + 4} y={m.t + 10} style={{ fill: "var(--fg-2)" }}>{marker.label}</text>
            </g>
          )}
          {hover != null && <line x1={X(hover)} x2={X(hover)} y1={m.t} y2={height - m.b} stroke="var(--fg-2)" strokeWidth={1} />}
        </svg>
      )}
      {hover != null && !table && (
        <div className="tip" style={{ left: Math.min((X(hover) / W) * (wrap.current?.clientWidth ?? W) + 12, (wrap.current?.clientWidth ?? W) - 190), top: 8 }}>
          <div className="h">{xf(x[hover]!)}</div>
          {bands?.filter((b) => b.label).map((b) => (
            <div className="r" key={b.label}>
              <span>{b.label}</span>
              <span className="num">{format(b.lo[hover] ?? NaN)} – {format(b.hi[hover] ?? NaN)}</span>
            </div>
          ))}
          {series.map((s) => (
            <div className="r" key={s.name}>
              <span><i style={{ background: s.color }} />{s.name}</span>
              <span className="num">{format(s.values[hover] ?? NaN)}</span>
            </div>
          ))}
        </div>
      )}
      <button className="btn" style={{ position: "absolute", right: 0, bottom: -4, padding: "0 6px", fontSize: 11, opacity: 0.7 }} onClick={() => setTable((t) => !t)}>
        {table ? "Chart" : "Table"}
      </button>
    </div>
  );
}

export function Legend({ items }: { items: { name: string; color: string }[] }) {
  return (
    <span style={{ display: "inline-flex", gap: 12, flexWrap: "wrap" }}>
      {items.map((i) => (
        <span key={i.name} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <i style={{ width: 12, height: 2, borderRadius: 1, background: i.color, display: "inline-block" }} />
          {i.name}
        </span>
      ))}
    </span>
  );
}
