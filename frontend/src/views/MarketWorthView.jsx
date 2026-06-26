import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

function fmtRange(min, max, currency) {
  const sym = { RUB: "₽", EUR: "€", USD: "$" }[currency] ?? currency;
  const fmt = (n) => n == null ? null : n.toLocaleString("ru-RU");
  if (min != null && max != null) return `${fmt(min)}–${fmt(max)} ${sym}/мес`;
  if (min != null) return `от ${fmt(min)} ${sym}/мес (потолок не найден)`;
  if (max != null) return `до ${fmt(max)} ${sym}/мес`;
  return "нет данных";
}

function SampleBar({ current, min }) {
  const pct = Math.min(100, (current / min) * 100);
  return (
    <div className="sample-bar-wrap">
      <div className="sample-bar">
        <div className="sample-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <span className="sample-bar-label">{current} из {min}</span>
    </div>
  );
}

function FreqList({ items, countKey = "count", nameKey = "tech", pctKey = "pct" }) {
  return (
    <div className="stack-list">
      {items.map((item) => (
        <div className="stack-row" key={item[nameKey]}>
          <span className="stack-tech">{item[nameKey]}</span>
          <div className="stack-bar-wrap">
            <div className="stack-bar-fill" style={{ width: `${Math.min(100, item[pctKey])}%` }} />
          </div>
          <span className="stack-pct">{item[pctKey]}%</span>
          <span className="stack-count">{item[countKey]}</span>
        </div>
      ))}
    </div>
  );
}

function CardTitle({ title, meta }) {
  return (
    <div className="mw-card-head">
      <span className="mw-card-title">{title}</span>
      {meta && <span className="mw-card-meta">{meta}</span>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Analytics cards
// ---------------------------------------------------------------------------

function SalaryCard({ data }) {
  const minSample = data?.min_sample ?? 3;
  if (!data) return null;
  return (
    <div className="mw-block-card">
      <CardTitle title="Зарплата" />
      {data.degraded && (
        <div className="market-worth-warning">⚠️ {data.degraded_reason}</div>
      )}
      <div className="market-worth-grid">
        <div className="market-worth-card">
          <div className="market-worth-label">🇷🇺 Россия</div>
          {data.ru_sample_size >= minSample ? (
            <div className="market-worth-range">
              {fmtRange(data.ru_min, data.ru_max, data.ru_currency)}
            </div>
          ) : (
            <div className="market-worth-accumulating">
              <div className="market-worth-range">копится…</div>
              <SampleBar current={data.ru_sample_size} min={minSample} />
            </div>
          )}
          <div className="mw-sample-note">n={data.ru_sample_size}</div>
        </div>
        <div className="market-worth-card">
          <div className="market-worth-label">🌍 Международный</div>
          {data.intl_sample_size >= minSample ? (
            <div className="market-worth-range">
              {fmtRange(data.intl_min, data.intl_max, data.intl_currency)}
            </div>
          ) : (
            <div className="market-worth-accumulating">
              <div className="market-worth-range">копится…</div>
              <SampleBar current={data.intl_sample_size} min={minSample} />
            </div>
          )}
          <div className="mw-sample-note">n={data.intl_sample_size}</div>
        </div>
      </div>
      {data.reasoning_short && (
        <p className="market-worth-reasoning">{data.reasoning_short}</p>
      )}
      <div className="market-worth-meta">
        P25–P75 · скор 50–100 · пороговый пул: {minSample}
      </div>

      <div className="market-worth-benchmark mw-benchmark-incard">
        <div className="market-worth-benchmark-title">Ориентир по рынку</div>
        <div className="market-worth-benchmark-subtitle">
          applied AI / LLM · middle · 06.2026
        </div>
        <div className="market-worth-benchmark-rows">
          <div className="market-worth-benchmark-row">
            <span className="market-worth-benchmark-flag">🇷🇺</span>
            <div>
              <div className="market-worth-benchmark-range">200 000 – 300 000 ₽</div>
              <div className="market-worth-benchmark-note">
                не ниже 200к · целиться 250–300к
              </div>
            </div>
          </div>
          <div className="market-worth-benchmark-row">
            <span className="market-worth-benchmark-flag">🌍</span>
            <div>
              <div className="market-worth-benchmark-range">$4 500 – 8 000</div>
              <div className="market-worth-benchmark-note">
                не ниже $4.5k · метить $5–7k<br />
                fine-tuning/PyTorch — не нужен
              </div>
            </div>
          </div>
        </div>
        <div className="market-worth-benchmark-sources">
          enigmai.ru · vc.ru/ai · hirehi.ru<br />
          ayautomate.com · kore1.com<br />
          remotelytalents.com · lemon.io
        </div>
        <div className="market-worth-benchmark-footer">
          26.06.2026 · обновлять раз в 2–3 мес
        </div>
      </div>
    </div>
  );
}

function StackCard({ sa }) {
  if (!sa) return (
    <div className="mw-block-card">
      <CardTitle title="Стек" />
      <div className="tab-empty">Нет данных</div>
    </div>
  );
  const { top_tech, total_pool, vacancies_with_stack, small_sample, degraded_reason } = sa;
  return (
    <div className="mw-block-card">
      <CardTitle title="Стек" meta={`n=${vacancies_with_stack} / пул ${total_pool}`} />
      {small_sample && <div className="market-worth-warning">{degraded_reason}</div>}
      {top_tech.length === 0
        ? <div className="stack-block-empty">Стек пока не собран</div>
        : <FreqList items={top_tech} nameKey="tech" pctKey="pct" countKey="count" />
      }
    </div>
  );
}

function BenefitsCard({ ba }) {
  if (!ba) return (
    <div className="mw-block-card">
      <CardTitle title="Бенефиты" />
      <div className="tab-empty">Нет данных</div>
    </div>
  );
  const { top_benefits, total_pool, vacancies_with_benefits, small_sample, degraded_reason } = ba;
  return (
    <div className="mw-block-card">
      <CardTitle title="Бенефиты" meta={`n=${vacancies_with_benefits} / пул ${total_pool}`} />
      {small_sample && <div className="market-worth-warning">{degraded_reason}</div>}
      {top_benefits.length === 0
        ? <div className="stack-block-empty">Бенефиты пока не собраны</div>
        : <FreqList items={top_benefits} nameKey="benefit" pctKey="pct" countKey="count" />
      }
    </div>
  );
}

function RequirementsCard({ ra }) {
  if (!ra) return (
    <div className="mw-block-card">
      <CardTitle title="Требования" />
      <div className="tab-empty">Нет данных</div>
    </div>
  );
  const {
    seniority_dist, remote_dist,
    relocation_count, relocation_pct,
    total_pool, vacancies_with_seniority,
    small_sample, degraded_reason,
  } = ra;
  return (
    <div className="mw-block-card">
      <CardTitle title="Требования" meta={`пул ${total_pool}`} />
      {small_sample && <div className="market-worth-warning">{degraded_reason}</div>}

      <div className="req-relocation-card">
        <div className="req-relocation-val">{relocation_pct}%</div>
        <div className="req-relocation-label">
          вакансий с релокацией
          <span className="req-relocation-abs"> ({relocation_count} из {total_pool})</span>
        </div>
      </div>

      <div className="req-section">
        <div className="stack-block-header">
          <span className="stack-block-title">Грейд</span>
          <span className="stack-block-meta">n={vacancies_with_seniority}</span>
        </div>
        {seniority_dist.length === 0
          ? <div className="stack-block-empty">Данных о грейде нет</div>
          : <FreqList items={seniority_dist} nameKey="grade" pctKey="pct" countKey="count" />
        }
      </div>

      <div className="req-section">
        <div className="stack-block-header">
          <span className="stack-block-title">Формат работы</span>
        </div>
        <FreqList items={remote_dist.filter(d => d.count > 0)} nameKey="label" pctKey="pct" countKey="count" />
      </div>
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

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  return (
    <div className="view market-worth-view">
      <div className="view-list">
        <div className="mw-header">
          <h1>Анализ рынка</h1>
          {data && (
            <span className="mw-header-n">n={data.total_relevant_vacancies} вакансий</span>
          )}
        </div>

        {loading && !data && <div className="loading">Загрузка…</div>}
        {error && <div className="error">{error}</div>}

        {data && (
          <div className="mw-grid">
            <SalaryCard data={data} />
            <StackCard sa={data.stack_analytics} />
            <BenefitsCard ba={data.benefits_analytics} />
            <RequirementsCard ra={data.requirements_analytics} />
          </div>
        )}
      </div>
    </div>
  );
}
