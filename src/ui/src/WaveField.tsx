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
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const ctx = el.getContext("2d");
    if (!ctx) return;
    let width = 1, height = 1, frame = 0, last = 0, visible = true;
    const draw = () => {
      ctx.clearRect(0, 0, width, height);
      const step = width < 600 ? 17 : 14;
      for (let y = -30; y < height + 30; y += step) {
        for (let x = 0; x < width; x += step) {
          const u = x / width, v = y / height;
          const wave = Math.sin(u * 11 + v * 5 + phase.current) * .55
            + Math.cos(v * 9 - u * 4 - phase.current * .65) * .45;
          const crest = Math.pow((wave + 1) / 2, 2);
          const fade = Math.min(1, u * 2) * Math.sin(Math.max(0, Math.min(1, v)) * Math.PI);
          ctx.fillStyle = `rgba(${143 + Math.round(crest * 30)}, ${183 + Math.round(crest * 28)}, 224, ${(.08 + crest * .48) * fade})`;
          ctx.beginPath();
          ctx.arc(x, y + wave * 24, .6 + crest * 2.3, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    };
    const tick = (now: number) => {
      if (now - last >= 32) {
        phase.current += last ? Math.min(now - last, 60) * .00024 : 0;
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
  }, [reduced]);
  // the field stops by itself under prefers-reduced-motion, off-screen and in a hidden tab
  return <canvas ref={canvas} className="wave-field" aria-hidden="true" />;
}
