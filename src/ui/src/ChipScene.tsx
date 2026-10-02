import type { CSSProperties } from "react";
import { BrandMark } from "./BrandMark";
import { seededRandom } from "./loadingQuips";

/** A small black board with the product chip turning in space, binary digits on tilted orbits (#598).
 *  Pure CSS 3D: one preserve-3d context, so the browser hides the far digits behind the board. Everything here is
 *  decoration (aria-hidden); the geometry is computed once per module, never per render. */

const LEADS = Array.from({ length: 8 }, (_, i) => 86 + i * 10);
const ROUTED = [0, 1, 3, 4, 6, 7];

/** Traces from the chip leads to pads at the board edge, on a 240-unit board. */
function traces(): { d: string; pad: [number, number] }[] {
  const out: { d: string; pad: [number, number] }[] = [];
  for (const i of ROUTED) {
    const o = LEADS[i];
    const s = Math.round(o + (o - 121) * 0.7);
    out.push({ d: `M66 ${o}H48L30 ${s}H15`, pad: [12, s] });
    out.push({ d: `M174 ${o}H192L210 ${s}H225`, pad: [228, s] });
    out.push({ d: `M${o} 66V48L${s} 30V15`, pad: [s, 12] });
    out.push({ d: `M${o} 174V192L${s} 210V225`, pad: [s, 228] });
  }
  return out;
}
const TRACES = traces();
const PULSES = [TRACES[2], TRACES[9], TRACES[19]];
const LEAD_MARKS = LEADS.flatMap((o) => [`M68 ${o}h6`, `M166 ${o}h6`, `M${o} 68v6`, `M${o} 166v6`]);
const HOLES: [number, number][] = [[18, 18], [222, 18], [18, 222], [222, 222]];
const VIAS = (() => {
  const rnd = seededRandom(17);
  return Array.from({ length: 46 }, () => [Math.round(26 + rnd() * 188), Math.round(26 + rnd() * 188)] as [number, number]);
})();

interface Orbit { r: number; tx: number; tz: number; dur: number; count: number; shift: number }
// the far side of an orbit with a negative tilt is its upper half, so its depth phase turns by half a circle
const ORBITS: Orbit[] = [
  { r: 178, tx: 72, tz: -16, dur: 26, count: 12, shift: 0.12 },
  { r: 206, tx: -64, tz: 22, dur: 34, count: 11, shift: 0.47 },
  { r: 158, tx: 80, tz: 6, dur: 21, count: 9, shift: 0.73 },
];
const GLYPHS = (() => {
  const rnd = seededRandom(1984);
  return ORBITS.map((o) => Array.from({ length: o.count }, (_, i) => ({
    bit: rnd() < 0.5 ? "0" : "1",
    a: (360 / o.count) * i + (rnd() - 0.5) * 9,
    phone: i % 3 !== 2,
  })));
})();

function Board() {
  return <svg className="pcb-art" viewBox="0 0 240 240" aria-hidden="true">
    <g className="pcb-traces">{TRACES.map((t, i) => <path key={i} d={t.d} />)}</g>
    <g className="pcb-pads">{TRACES.map((t, i) => <circle key={i} cx={t.pad[0]} cy={t.pad[1]} r="2.6" />)}</g>
    <g className="pcb-leads">{LEAD_MARKS.map((d, i) => <path key={i} d={d} />)}</g>
    <g className="pcb-holes">{HOLES.map(([x, y], i) => <circle key={i} cx={x} cy={y} r="5" />)}</g>
    <g className="pcb-pulses">{PULSES.map((t, i) => <path key={i} className={`pcb-pulse pcb-pulse-${i}`} d={t.d} pathLength="100" />)}</g>
    <text className="pcb-silk" x="206" y="40" textAnchor="end">MTR-01</text>
  </svg>;
}

function BoardBack() {
  return <svg className="pcb-art" viewBox="0 0 240 240" aria-hidden="true">
    <g className="pcb-vias">{VIAS.map(([x, y], i) => <circle key={i} cx={x} cy={y} r="1.6" />)}</g>
    <g className="pcb-holes">{HOLES.map(([x, y], i) => <circle key={i} cx={x} cy={y} r="5" />)}</g>
    <rect className="pcb-back-frame" x="70" y="70" width="100" height="100" rx="4" />
    <text className="pcb-silk" x="120" y="196" textAnchor="middle">МИТРАНДИР</text>
  </svg>;
}

export function ChipScene() {
  return <div className="chip-scene-box" aria-hidden="true">
    <div className="chip-shadow" />
    <div className="chip-scene">
      <div className="chip-float">
        <div className="chip-world">
          <div className="chip-spin">
            <div className="pcb-face pcb-front"><Board /><span className="pcb-glint" /></div>
            <div className="pcb-face pcb-back"><BoardBack /></div>
            <div className="pcb-edge pcb-edge-r" /><div className="pcb-edge pcb-edge-l" />
            <div className="pcb-edge pcb-edge-t" /><div className="pcb-edge pcb-edge-b" />
            <div className="ic">
              <div className="ic-face ic-top"><BrandMark className="ic-mark" /></div>
              <div className="ic-side ic-side-r" /><div className="ic-side ic-side-l" />
              <div className="ic-side ic-side-t" /><div className="ic-side ic-side-b" />
            </div>
          </div>
          {ORBITS.map((o, k) => {
            const ring = { "--r": `${o.r}px`, "--tx": `${o.tx}deg`, "--tz": `${o.tz}deg`, "--dur": `${o.dur}s`,
              "--delay": `${(-o.shift * o.dur).toFixed(2)}s` } as CSSProperties;
            return <div key={k} className={`orbit orbit-${k}`} style={ring}>
              {GLYPHS[k].map((g, i) => {
                const phase = ((g.a + (o.tx < 0 ? 180 : 0)) / 360 + o.shift) * o.dur;
                return <span key={i} className="orb" data-phone={g.phone ? undefined : "off"} style={{ "--a": `${g.a.toFixed(2)}deg` } as CSSProperties}>
                  <span className="orb-face"><span className="orb-glyph" style={{ animationDelay: `${(-phase).toFixed(2)}s` }}>{g.bit}</span></span>
                </span>;
              })}
            </div>;
          })}
        </div>
      </div>
    </div>
  </div>;
}
