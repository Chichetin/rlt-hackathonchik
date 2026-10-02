import { useEffect, useState } from "react";
import PatternWaves from "./vendor/react-bits/PatternWaves";
import MicroSlats from "./vendor/react-bits/MicroSlats";

/** The reference components themselves; artwork settings are fixed per route. */
export function WaveField({ pattern = "waves", calm = false }: { pattern?: "waves" | "slats"; calm?: boolean }) {
  const [reduced, setReduced] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  // #640: the light theme recolours the artwork instead of hiding it
  const [light, setLight] = useState(() => document.documentElement.dataset.theme === "light");
  useEffect(() => {
    const html = document.documentElement;
    const mo = new MutationObserver(() => setLight(html.dataset.theme === "light"));
    mo.observe(html, { attributes: true, attributeFilter: ["data-theme"] });
    return () => mo.disconnect();
  }, []);
  const bg = light ? "#ffffff" : "#030305";
  return <div className={`wave-field wave-field-${pattern}`} data-pattern={pattern} aria-hidden="true">
    {pattern === "waves"
      ? <PatternWaves key={`${reduced}-${light}`} preset="silk" color={light ? "#1f3a6b" : "#ffffff"} backgroundColor={bg} fade="edges" interactive={!reduced} paused={reduced} intro={!reduced} />
      : <MicroSlats key={`${reduced || calm}-${light}`} preset="swell" color={light ? "#8d9ab3" : "#777984"} glintColor={light ? "#1f3a6b" : "#d9dbe5"} backgroundColor={bg} interactive={!reduced && !calm} paused={reduced || calm} intro={!reduced && !calm} />}
  </div>;
}
