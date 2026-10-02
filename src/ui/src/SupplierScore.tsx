import { levelLabel, levelOf, RelevanceTag } from "./roles";

/** A matching score, not a probability or a supplier quality rating. */
export function SupplierScore({ score, inspector = false }: { score: number; inspector?: boolean }) {
  return <span className={`supplier-score ${inspector ? "inspector-score" : "score"} score-level-${levelOf(score)}`}
    title="Итоговая оценка соответствия закупке"
    aria-label={`Оценка соответствия: ${score.toFixed(2)}, релевантность ${levelLabel(levelOf(score))}`}>
    <span className="supplier-score-label">Релевантность</span>
    <strong className="score-num">{score.toFixed(2)}</strong>
    <RelevanceTag score={score} />
    <span className="score-meter" aria-hidden="true">
      <span style={{ transform: `scaleX(${Math.max(0, Math.min(1, score))})` }} />
    </span>
  </span>;
}
