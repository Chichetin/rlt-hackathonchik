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
  return <div className={`wave-field wave-field-${pattern}`} data-pattern={pattern} aria-hidden="true">
    {pattern === "waves"
      ? <PatternWaves key={String(reduced)} preset="silk" color="#ffffff" backgroundColor="#030305" fade="edges" interactive={!reduced} paused={reduced} intro={!reduced} />
      : <MicroSlats key={String(reduced || calm)} preset="swell" color="#777984" glintColor="#d9dbe5" backgroundColor="#030305" interactive={!reduced && !calm} paused={reduced || calm} intro={!reduced && !calm} />}
  </div>;
}
