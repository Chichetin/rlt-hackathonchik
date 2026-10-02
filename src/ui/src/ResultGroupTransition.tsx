import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { animate, useReducedMotion } from "motion/react";
import { easeOut } from "./Motion";

/** Both scroll containers stay mounted; only their visibility changes at the fade midpoint. */
export function ResultGroupTransition({ group, history, discovered }: {
  group: "history" | "new"; history: ReactNode; discovered: ReactNode;
}) {
  const [displayed, setDisplayed] = useState(group);
  const body = useRef<HTMLDivElement>(null);
  const entered = useRef(group);
  const reduced = useReducedMotion();
  const leaving = displayed !== group;

  useLayoutEffect(() => {
    if (!leaving) return;
    if (reduced || !body.current) { setDisplayed(group); return; }
    let current = true;
    const exit = animate(body.current, { opacity: .04, y: -3 }, { duration: .08, ease: "easeOut" });
    void exit.then(() => { if (current) setDisplayed(group); });
    return () => { current = false; exit.stop(); };
  }, [group, leaving, reduced]);

  useLayoutEffect(() => {
    if (leaving || !body.current) return;
    const changed = entered.current !== displayed;
    entered.current = displayed;
    const enter = animate(body.current,
      changed && !reduced ? { opacity: [.04, 1], y: [8, 0] } : { opacity: 1, y: 0 },
      { duration: reduced ? 0 : .24, ease: easeOut });
    return () => enter.stop();
  }, [displayed, leaving, reduced]);

  return <div ref={body} className="result-list-transition" inert={leaving} aria-hidden={leaving || undefined} data-displayed-group={displayed}>
    <div className="result-list-scroll" hidden={displayed !== "history"}>{history}</div>
    <div className="result-list-scroll" hidden={displayed !== "new"}>{discovered}</div>
  </div>;
}
