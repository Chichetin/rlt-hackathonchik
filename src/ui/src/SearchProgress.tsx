import { useEffect, useState } from "react";

export function SearchProgress() {
  const [slow, setSlow] = useState(false);
  useEffect(() => { const timer = window.setTimeout(() => setSlow(true), 6000); return () => clearTimeout(timer); }, []);
  return <div className="search-progress" role="status" aria-live="polite">
    <svg className="search-sigil" width="68" height="68" viewBox="0 0 64 64" fill="none" aria-hidden="true">
      <path className="sigil-outline" d="M32 3 61 32 32 61 3 32Z" />
      <path className="sigil-trace" pathLength="100" d="M32 3 61 32 32 61 3 32Z" />
      <path className="sigil-mark" d="M18 44V21l14 17 14-17v23M14 44h9m18 0h9" />
      <circle className="sigil-core" cx="32" cy="18" r="2" />
    </svg>
    <div><strong>Подбираем поставщиков</strong><p>{slow
      ? "Поиск ещё идёт. На большой объём данных может потребоваться больше времени. Результаты появятся здесь."
      : "Ищем компании по вашему запросу. Результаты появятся ниже."}</p></div>
  </div>;
}
