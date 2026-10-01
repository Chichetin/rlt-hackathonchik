import { useEffect, useRef, useState } from "react";

/** Original Canvas2D field: moving dotted contours, without image or shader assets. */
export function WaveField() {
  const canvas = useRef<HTMLCanvasElement>(null);
  const phase = useRef(0);
  const [reduced, setReduced] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  const [pattern, setPattern] = useState<"waves" | "slats">("waves");
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const ctx = el.getContext("2d");
    if (!ctx) return;
    let width = 1, height = 1, frame = 0, last = 0, visible = true;
    const draw = () => {
      ctx.clearRect(0, 0, width, height);
      const step = width < 600 ? 12 : 10;
      const rowStep = pattern === "slats" ? step * 2.5 : step;
      for (let y = -30; y < height + 30; y += rowStep) {
        for (let x = 0; x < width; x += step) {
          const u = x / width, v = y / height;
          const wave = Math.sin(u * 11 + v * 5 + phase.current) * .55
            + Math.cos(v * 9 - u * 4 - phase.current * .65) * .45;
          const crest = Math.pow((wave + 1) / 2, 2.5);
          const fade = (.4 + .6 * u) * Math.sin(Math.max(0, Math.min(1, v)) * Math.PI);
          ctx.fillStyle = `rgba(222, 225, 223, ${(.06 + crest * .85) * fade})`;
          ctx.beginPath();
          if (pattern === "slats") {
            ctx.roundRect(x, y, step * .65, rowStep * .82, 2);
          } else {
            ctx.arc(x, y + wave * 24, .45 + crest * 2.6, 0, Math.PI * 2);
          }
          ctx.fill();
        }
      }
    };
    const tick = (now: number) => {
      if (now - last >= 32) {
        phase.current += last ? Math.min(now - last, 60) * .00038 : 0;
        last = now;
        draw();
      }
      frame = requestAnimationFrame(tick);
    };
    const sync = () => {
      cancelAnimationFrame(frame);
      last = 0;
      draw();
      if (!reduced && visible && !document.hidden) frame = requestAnimationFrame(tick);
    };
    const resize = new ResizeObserver(() => {
      const rect = el.getBoundingClientRect();
      width = rect.width; height = rect.height;
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      el.width = Math.round(width * dpr); el.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      sync();
    });
    const observer = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; sync(); });
    resize.observe(el); observer.observe(el);
    document.addEventListener("visibilitychange", sync);
    return () => {
      cancelAnimationFrame(frame); resize.disconnect(); observer.disconnect();
      document.removeEventListener("visibilitychange", sync);
    };
  }, [reduced, pattern]);
  // the field stops by itself under prefers-reduced-motion, off-screen and in a hidden tab (no pause button, #171)
  return <>
    <canvas ref={canvas} className="wave-field" aria-hidden="true" />
    <div className="wave-tools">
    <div className="wave-patterns" role="group" aria-label="Рисунок фона">
      <button type="button" aria-pressed={pattern === "waves"} onClick={() => setPattern("waves")}>Волны</button>
      <button type="button" aria-pressed={pattern === "slats"} onClick={() => setPattern("slats")}>Рёбра</button>
    </div>
    </div>
  </>;
}
