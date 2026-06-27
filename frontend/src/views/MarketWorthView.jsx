import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// ---------------------------------------------------------------------------
// Color scale for tiles — darkest = highest normalized value
// ---------------------------------------------------------------------------

const TEAL_STOPS = [
  { bg: "#C3EBDC", text: "#04342C" },
  { bg: "#7FD3B5", text: "#04342C" },
  { bg: "#5DAE91", text: "#04342C" },
  { bg: "#2E8B6E", text: "#E1F5EE" },
  { bg: "#0F6E56", text: "#E1F5EE" },
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
// Salary scale bar — positions P25-P75 segment on a fixed reference scale
// ---------------------------------------------------------------------------

function SalaryScale({ p25, p75, currency, sampleSize, total }) {
  const scaleMax = SCALE_MAX[currency] ?? 600000;
  const p75real = p75 ?? p25;
  const leftPct = Math.max(0, Math.min(100, (p25 / scaleMax) * 100));
  const widthPct = Math.max(2, Math.min(100 - leftPct, ((p75real - p25) / scaleMax) * 100));
  const sym = { RUB: "₽", USD: "$", EUR: "€" }[currency] ?? currency;
  const shortSym = currency === "RUB" ? "к₽" : (currency === "USD" ? "k$" : "k€");

  return (
    <div className="mw-salary-scale-wrap">
      <div className="mw-salary-scale">
        <div className="mw-salary-scale-seg" style={{ left: `${leftPct}%`, width: `${widthPct}%` }} />
      </div>
      <div className="mw-salary-caption">
        P25 {fmtShort(p25, currency)}{shortSym} · P75 {fmtShort(p75real, currency)}{shortSym}
        {" · "}{sampleSize} из {total} с зарплатой
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top block: salary (left) + divider + benchmark (right)
// ---------------------------------------------------------------------------

function TopBlock({ data }) {
  const minSample = data.min_sample ?? 3;
  const hasRu = data.ru_sample_size >= minSample && data.ru_min != null;
  const hasIntl = data.intl_sample_size >= minSample && data.intl_min != null;

  return (
    <div className="mw-top-card">

      {/* ── Left: salary ── */}
      <div className="mw-salary-col">
        <div className="mw-section-label">Зарплата</div>

        {data.degraded && !hasRu && !hasIntl && (
          <div className="market-worth-warning">{data.degraded_reason}</div>
        )}

        {/* RU row */}
        <div className="mw-salary-row">
          <span className="mw-salary-flag">🇷🇺</span>
          <div className="mw-salary-body">
            {hasRu ? (
              <>
                <div className="mw-salary-range">{fmtRange(data.ru_min, data.ru_max, data.ru_currency)}</div>
                <SalaryScale
                  p25={data.ru_min}
                  p75={data.ru_max}
                  currency={data.ru_currency}
                  sampleSize={data.ru_sample_size}
                  total={data.total_relevant_vacancies}
                />
              </>
            ) : (
              <div className="mw-salary-accumulating">
                копится… ({data.ru_sample_size} / {minSample})
              </div>
            )}
          </div>
        </div>

        {/* Intl row */}
        <div className="mw-salary-row">
          <span className="mw-salary-flag">🌍</span>
          <div className="mw-salary-body">
            {hasIntl ? (
              <>
                <div className="mw-salary-range">{fmtRange(data.intl_min, data.intl_max, data.intl_currency)}</div>
                <SalaryScale
                  p25={data.intl_min}
                  p75={data.intl_max}
                  currency={data.intl_currency}
                  sampleSize={data.intl_sample_size}
                  total={data.total_relevant_vacancies}
                />
              </>
            ) : (
              <div className="mw-salary-accumulating">
                копится… ({data.intl_sample_size} / {minSample})
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="mw-vdivider" />

      {/* ── Right: benchmark ── */}
      <div className="mw-benchmark-col">
        <div className="mw-section-label">Ориентир по рынку</div>
        <div className="mw-bench-subtitle">applied AI / LLM · middle · 06.2026</div>

        <div className="mw-bench-row">
          <span className="mw-bench-flag">🇷🇺</span>
          <div>
            <div className="mw-bench-range">200 000 – 300 000 ₽</div>
            <div className="mw-bench-note">не ниже 200к · целиться 250–300к</div>
          </div>
        </div>

        <div className="mw-bench-row">
          <span className="mw-bench-flag">🌍</span>
          <div>
            <div className="mw-bench-range">$4 500 – 8 000</div>
            <div className="mw-bench-note">не ниже $4.5k · метить $5–7k</div>
          </div>
        </div>

        <div className="mw-bench-aside">fine-tuning / PyTorch — не нужен</div>

        <div className="mw-bench-sources">
          enigmai.ru · vc.ru/ai · hirehi.ru<br />
          ayautomate.com · kore1.com<br />
          remotelytalents.com · lemon.io
        </div>
        <div className="mw-bench-footer">26.06.2026 · обновлять раз в 2–3 мес</div>
      </div>

    </div>
  );
}

// ---------------------------------------------------------------------------
// Frequency tile block — 2×2 grid of top-4 items
// ---------------------------------------------------------------------------

function FreqTileBlock({ title, n, items }) {
  const top4 = items.slice(0, 4);
  const rest = items.length - 4;
  const maxPct = top4.length > 0 ? top4[0].pct : 0;

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

      {rest > 0 && <div className="mw-tile-more">+ ещё {rest}</div>}
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
        <div className="mw-header">
          <h1>Анализ рынка</h1>
          {data && <span className="mw-header-n">n={data.total_relevant_vacancies} вакансий</span>}
        </div>

        {loading && !data && <div className="loading">Загрузка…</div>}
        {error && <div className="error">{error}</div>}

        {data && (
          <>
            <TopBlock data={data} />

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
