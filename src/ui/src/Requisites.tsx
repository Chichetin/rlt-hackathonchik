import type { CounterpartyMatch } from "./api";
import { okpd2Text, useOkpd2Labels } from "./okpd2";

type Cp = CounterpartyMatch["counterparty"];

/** ОГРН показываем только если он есть среди фактов с источником (Evidence.field = "ogrn"). */
export function ogrnOf(cp: Cp): string | null {
  const e = (cp.evidence ?? []).find((x) => x.field === "ogrn" && x.value && x.url);
  return e ? String(e.value) : null;
}

export function okpd2CodesOf(cp: Cp): string[] {
  const raw = cp.stats?.top_okpd2 ?? cp.stats?.okpd2_classes ?? "";
  return String(raw).split(",").map((c) => c.trim()).filter(Boolean);
}

export function Requisites({ cp }: { cp: Cp }) {
  const codes = okpd2CodesOf(cp);
  const labels = useOkpd2Labels(codes);
  const ogrn = ogrnOf(cp);
  const russian = !cp.country || cp.country === "RU";
  if (!cp.inn && !cp.kpp && !ogrn && !codes.length) return null;
  return (
    <section className="requisites" aria-label="Реквизиты">
      <h3>Реквизиты</h3>
      <dl className="facts">
        {cp.inn && russian && (<><dt>ИНН</dt><dd>{cp.inn}</dd></>)}
        {cp.kpp && (<><dt>КПП</dt><dd>{cp.kpp}</dd></>)}
        {ogrn && (<><dt>ОГРН</dt><dd>{ogrn}</dd></>)}
        {codes.length > 0 && (
          <>
            <dt>ОКПД2</dt>
            <dd>
              {codes.map((c) => (
                <span key={c} style={{ display: "block" }}>{okpd2Text([c], labels)}</span>
              ))}
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}
