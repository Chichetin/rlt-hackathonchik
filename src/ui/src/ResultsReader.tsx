import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

/** Change only the reading layout: the result list, selection and detail body stay mounted. */
export function ResultsReader({ children }: { children: ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const [fullLength, setFullLength] = useState(false);
  const reader = useRef<HTMLDivElement>(null);
  const expandButton = useRef<HTMLButtonElement>(null);
  const positions = useRef<{ element: HTMLElement; top: number }[]>([]);
  const pagePosition = useRef(0);
  const restore = useRef(false);
  const returnFocus = useRef(false);

  function rememberColumns() {
    if (fullLength) return;
    positions.current = Array.from(reader.current?.querySelectorAll<HTMLElement>(".result-list-scroll, .result-inspector") ?? [])
      .map(element => ({ element, top: element.scrollTop }));
  }
  function toggleExpanded() {
    rememberColumns();
    if (!expanded) pagePosition.current = window.scrollY;
    restore.current = true;
    returnFocus.current = expanded;
    setExpanded(!expanded);
  }
  function toggleLength() {
    rememberColumns();
    restore.current = true;
    setFullLength(!fullLength);
  }
  useLayoutEffect(() => {
    if (!restore.current) return;
    restore.current = false;
    if (!fullLength) positions.current.forEach(({ element, top }) => { element.scrollTop = top; });
    if (expanded) window.scrollTo({ top: 0, behavior: "instant" });
    if (returnFocus.current) {
      returnFocus.current = false;
      window.scrollTo({ top: pagePosition.current, behavior: "instant" });
      expandButton.current?.focus({ preventScroll: true });
    }
  }, [expanded, fullLength]);

  useEffect(() => {
    if (!expanded) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented || document.querySelector('[role="dialog"]')) return;
      event.preventDefault();
      toggleExpanded();
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  });

  return <div ref={reader} className="results-reader" data-expanded={expanded} data-full-length={fullLength}>
    <div className="results-reader-tools" role="group" aria-label="Вид результатов">
      <span className="reader-caption">Просмотр поставщиков</span>
      <button type="button" className="secondary" aria-pressed={fullLength} onClick={toggleLength}
        title="Показать списки и подробности целиком, с общей прокруткой">
        Без внутренней прокрутки
      </button>
      <button ref={expandButton} type="button" className="secondary reader-expand" aria-expanded={expanded} onClick={toggleExpanded}>
        <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
          <path d={expanded ? "M2 7h5V2m6 0v5h5M2 13h5v5m6 0v-5h5" : "M2 7V2h5m6 0h5v5M2 13v5h5m6 0h5v-5"} />
        </svg>
        {expanded ? "Свернуть" : "На весь экран"}
      </button>
    </div>
    {children}
  </div>;
}
