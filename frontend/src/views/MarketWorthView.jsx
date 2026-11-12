import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// ---------------------------------------------------------------------------
// Color scale for tiles — uses project CSS custom properties
// ---------------------------------------------------------------------------

const TEAL_STOPS = [
  { bg: "var(--teal-bri)", text: "var(--bg)" },          // lowest pct — lightest
  { bg: "var(--teal)",      text: "var(--bg)" },
  { bg: "var(--teal-dim)",  text: "var(--ink)" },
  { bg: "var(--teal-dim)",  text: "var(--teal-bri)" },
  { bg: "var(--teal-deep)", text: "var(--teal-bri)" },   // highest pct — darkest
];

function tileColor(pct, maxPct) {
  if (!maxPct) return TEAL_STOPS[0];
  const t = pct / maxPct;
  return TEAL_STOPS[Math.min(4, Math.floor(t * 5))];
}

// ---------------------------------------------------------------------------
// Salary formatting helpers
// ---------------------------------------------------------------------------

// Fixed scale max per currency for the P25-P75 bar positioning
const SCALE_MAX = { RUB: 600000, USD: 12000, EUR: 10000 };

function fmtRange(p25, p75, currency) {
  const sym = { RUB: "₽", USD: "$", EUR: "€" }[currency] ?? currency;
  const loc = (n) => n?.toLocaleString("ru-RU") ?? "?";
  if (currency === "RUB") return `${loc(p25)} – ${loc(p75 ?? p25)} ${sym}`;
  return `${sym}${loc(p25)} – ${sym}${loc(p75 ?? p25)}`;
}

function fmtShort(n, currency) {
  if (n == null) return "?";
  if (currency === "RUB") return n >= 1000 ? `${Math.round(n / 1000)}к` : String(n);
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

// ---------------------------------------------------------------------------
// Salary scale bar — always renders the track; fills only when data present
// ---------------------------------------------------------------------------

function SalaryScale({ p25, p75, median, currency, sampleSize, total, minSample }) {
  const hasData = p25 != null;
  const scaleMax = SCALE_MAX[currency] ?? 600000;
  const p75real = p75 ?? p25;
  const shortSym = currency === "RUB" ? "к₽" : currency === "USD" ? "k$" : "k€";

  // When accumulating: bar shows data-collection progress (sampleSize/minSample)
  const accPct = minSample > 0 ? Math.min(100, (sampleSize / minSample) * 100) : 0;

  const leftPct = hasData ? Math.max(0, Math.min(100, (p25 / scaleMax) * 100)) : 0;
  const widthPct = hasData
    ? Math.max(2, Math.min(100 - leftPct, ((p75real - p25) / scaleMax) * 100))
    : 0;

  return (
    <div className="mw-salary-scale-wrap">
      <div className="mw-salary-scale">
        {hasData
          ? <div className="mw-salary-scale-seg" style={{ left: `${leftPct}%`, width: `${widthPct}%` }} />
          : <div className="mw-salary-scale-seg mw-salary-scale-seg--acc" style={{ left: "0%", width: `${accPct}%` }} />
        }
      </div>
      <div className={`mw-salary-caption${hasData ? "" : " mw-salary-caption--acc"}`}>
        {hasData
          ? `P25 ${fmtShort(p25, currency)}${shortSym}${median != null ? ` · мед. ${fmtShort(median, currency)}${shortSym}` : ""} · P75 ${fmtShort(p75real, currency)}${shortSym} · ${sampleSize} из ${total} с зарплатой`
          : `копится… (${sampleSize} / ${minSample})`}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top block: salary (left) + divider + benchmark (right)
// ---------------------------------------------------------------------------

// What the international figure is made of: the geo guard never judged most of these vacancies, so the pool
// includes country-restricted ones. Says so under the number instead of leaving it to be read as "available".
const SOURCE_LABELS = { hn_hiring: "HN Hiring", wwr: "WeWorkRemotely", jobicy: "Jobicy" };

export function poolCaption(data) {
  const pct = (x) => `${Math.round(x * 100)}%`;
  const parts = [`Международный пул: ${data.intl_sample_size} вакансий`];
  if (data.intl_usd_share != null) parts.push(`${pct(data.intl_usd_share)} в USD`);
  if (data.intl_top_source) {
    parts.push(`${SOURCE_LABELS[data.intl_top_source] ?? data.intl_top_source} ${pct(data.intl_top_source_share ?? 0)}`);
  }
  return `${parts.join(" · ")} · гео-доступность не проверена у ${data.intl_geo_unchecked}: включены вакансии с ограничением по стране, цифра отражает рынок, а не доступные вам вакансии`;
}

function TopBlock({ data, card }) {
  const benchmarks = Array.isArray(card?.benchmarks) ? card.benchmarks : [];
  const sources = Array.isArray(card?.sources) ? card.sources : [];
  const minSample = data.min_sample ?? 3;
  const hasRu = data.ru_sample_size >= minSample && data.ru_min != null;
  const hasIntl = data.intl_sample_size >= minSample && data.intl_min != null;

  return (
    <div className="mw-top-card">

      {/* ── Left: salary ── */}
      <div className="mw-salary-col">
        <div className="mw-section-label">Зарплата</div>

        {data.degraded && !hasRu && !hasIntl && (
          <div className="market-worth-warning" style={{ whiteSpace: "pre-line" }}>
            {(data.degraded_reason ?? "").replace(/;\s*/g, ";\n")}
          </div>
        )}

        {/* RU row — scale always renders */}
        <div className="mw-salary-row">
          <span className="mw-salary-flag">🇷🇺</span>
          <div className="mw-salary-body">
            {hasRu && (
              <div className="mw-salary-range">{fmtRange(data.ru_min, data.ru_max, data.ru_currency)}</div>
            )}
            <SalaryScale
              p25={hasRu ? data.ru_min : null}
              p75={hasRu ? data.ru_max : null}
              median={hasRu ? data.ru_median : null}
              currency={data.ru_currency ?? "RUB"}
              sampleSize={data.ru_sample_size}
              total={data.total_relevant_vacancies}
              minSample={minSample}
            />
          </div>
        </div>

        {/* Intl row — scale always renders */}
        <div className="mw-salary-row">
          <span className="mw-salary-flag">🌍</span>
          <div className="mw-salary-body">
            {hasIntl && (
              <div className="mw-salary-range">{fmtRange(data.intl_min, data.intl_max, data.intl_currency)}</div>
            )}
            <SalaryScale
              p25={hasIntl ? data.intl_min : null}
              p75={hasIntl ? data.intl_max : null}
              median={hasIntl ? data.intl_median : null}
              currency={data.intl_currency ?? "USD"}
              sampleSize={data.intl_sample_size}
              total={data.total_relevant_vacancies}
              minSample={minSample}
            />
          </div>
        </div>
        {data.intl_sample_size > 0 && data.intl_geo_unchecked > 0 && (
          <div className="mw-bench-note" data-testid="mw-intl-pool">
            {poolCaption(data)}
          </div>
        )}
        {data.heuristic_note && <div className="mw-bench-note" title={data.heuristic_note}>эвристика: период по порогам, gross/net не нормализован</div>}
      </div>

      {/* ── Right: benchmark — private figures come from the profile (GET /api/profile-card); hidden without them ── */}
      {benchmarks.length > 0 && (
        <>
          <div className="mw-vdivider" />
          <div className="mw-benchmark-col">
            <div className="mw-section-label">Ориентир по рынку</div>
            {card.subtitle && <div className="mw-bench-subtitle">{card.subtitle}</div>}

            {benchmarks.map((b, i) => (
              <div key={i} className="mw-bench-row">
                <span className="mw-bench-flag">{b.label}</span>
                <div>
                  <div className="mw-bench-range">{b.value}</div>
                  {b.note && <div className="mw-bench-note">{b.note}</div>}
                </div>
              </div>
            ))}

            {sources.length > 0 && (
              <div className="mw-bench-chips">
                {sources.map((s) => (
                  <span key={s} className="mw-bench-chip">{s}</span>
                ))}
              </div>
            )}
          </div>
        </>
      )}

    </div>
  );
}

// ---------------------------------------------------------------------------
// Frequency tile block — 2×2 top tiles + expandable horizontal bars for rest
// ---------------------------------------------------------------------------

function FreqTileBlock({ title, n, items }) {
  const [expanded, setExpanded] = useState(false);
  const top4 = items.slice(0, 4);
  const rest = items.slice(4);
  const maxPct = top4.length > 0 ? top4[0].pct : 0;
  const hasMore = rest.length > 0;

  return (
    <div className="mw-ftblock">
      <div className="mw-ftblock-head">
        <span className="mw-ftblock-title">{title}</span>
        {n != null && <span className="mw-ftblock-n">n={n}</span>}
      </div>

      <div className="mw-tile-grid">
        {top4.map((item) => {
          const { bg, text } = tileColor(item.pct, maxPct);
          return (
            <div key={item.name} className="mw-tile" style={{ backgroundColor: bg, color: text }}>
              <span className="mw-tile-label">{item.name}</span>
              <span className="mw-tile-pct">{item.pct}%</span>
            </div>
          );
        })}
        {Array.from({ length: Math.max(0, 4 - top4.length) }).map((_, i) => (
          <div key={`empty-${i}`} className="mw-tile mw-tile--empty" />
        ))}
      </div>

      {hasMore && expanded && (
        <div className="mw-tile-bars">
          {rest.map((item) => (
            <div key={item.name} className="mw-tile-bar">
              <span className="mw-tile-bar-label">{item.name}</span>
              <div className="mw-tile-bar-track">
                <div
                  className="mw-tile-bar-fill"
                  style={{ width: `${maxPct > 0 ? (item.pct / maxPct) * 100 : 0}%` }}
                />
              </div>
              <span className="mw-tile-bar-pct">{item.pct}%</span>
            </div>
          ))}
        </div>
      )}

      {hasMore && (
        <button className="mw-tile-more" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "свернуть ↑" : `+ ещё ${rest.length}`}
        </button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Root view
// ---------------------------------------------------------------------------

export default function MarketWorthView() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getMarketWorth();
      setData(result);
    } catch (e) {
      setError(e.message || "Ошибка загрузки");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  // Private benchmark figures come from the profile; a failed or empty card just hides the block.
  const [card, setCard] = useState({ benchmarks: [], subtitle: "", sources: [] });
  useEffect(() => {
    let cancelled = false;
    api.getProfileCard()
      .then((c) => { if (!cancelled && c) setCard(c); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const stackItems = data?.stack_analytics?.top_tech
    ?.map((t) => ({ name: t.tech, pct: t.pct })) ?? [];

  // seniority_dist is sorted by grade order; re-sort by pct desc for tiles
  const gradeItems = (data?.requirements_analytics?.seniority_dist ?? [])
    .slice()
    .sort((a, b) => b.pct - a.pct)
    .map((t) => ({ name: t.grade, pct: t.pct }));

  const benefitsItems = data?.benefits_analytics?.top_benefits
    ?.map((t) => ({ name: t.benefit, pct: t.pct })) ?? [];

  // Format block: independent frequency counts per vacancy pool.
  // remote/hybrid/office from remote_dist; relocation from relocation_pct.
  // Each is count/total_pool — sums don't add to 100%.
  const formatItems = (() => {
    const ra = data?.requirements_analytics;
    if (!ra) return [];
    const byKey = Object.fromEntries((ra.remote_dist ?? []).map((d) => [d.key, d]));
    return [
      { name: "Удалёнка", pct: byKey.remote?.pct ?? 0 },
      { name: "Гибрид",   pct: byKey.hybrid?.pct ?? 0 },
      { name: "Офис",     pct: byKey.office?.pct ?? 0 },
      { name: "Релокация", pct: ra.relocation_pct ?? 0 },
    ].sort((a, b) => b.pct - a.pct);
  })();

  return (
    <div className="view market-worth-view">
      <div className="view-list">
        <h1>Анализ рынка</h1>

        {loading && !data && <div className="loading">Загрузка…</div>}
        {error && <div className="error">{error}</div>}

        {data && (
          <>
            <TopBlock data={data} card={card} />

            <div className="mw-bottom-grid">
              <FreqTileBlock
                title="Стек"
                n={data.stack_analytics?.vacancies_with_stack}
                items={stackItems}
              />
              <FreqTileBlock
                title="Грейд"
                n={data.requirements_analytics?.vacancies_with_seniority}
                items={gradeItems}
              />
              <FreqTileBlock
                title="Бенефиты"
                n={data.benefits_analytics?.vacancies_with_benefits}
                items={benefitsItems}
              />
              <FreqTileBlock
                title="Формат"
                n={data.requirements_analytics?.total_pool}
                items={formatItems}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
