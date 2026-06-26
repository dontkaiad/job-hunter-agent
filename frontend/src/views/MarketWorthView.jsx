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

function FreqBlockHeader({ title, n, pool }) {
  return (
    <div className="stack-block-header">
      <span className="stack-block-title">{title}</span>
      <span className="stack-block-meta">n={n} вакансий · пул {pool}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Зарплата
// ---------------------------------------------------------------------------

function SalaryTab({ data }) {
  const minSample = data?.min_sample ?? 3;
  if (!data) return null;
  return (
    <>
      <div className="market-worth-stats">
        <div className="market-worth-stat">
          <span className="market-worth-stat-val">{data.total_relevant_vacancies}</span>
          <span className="market-worth-stat-label">вакансий со скором 50–100</span>
        </div>
        <div className="market-worth-stat">
          <span className="market-worth-stat-val">{data.ru_sample_size}</span>
          <span className="market-worth-stat-label">с зарплатой РФ</span>
        </div>
        <div className="market-worth-stat">
          <span className="market-worth-stat-val">{data.intl_sample_size}</span>
          <span className="market-worth-stat-label">с зарплатой межд.</span>
        </div>
      </div>

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
        </div>
      </div>

      {data.reasoning_short && (
        <p className="market-worth-reasoning">{data.reasoning_short}</p>
      )}

      <div className="market-worth-meta">
        Диапазон P25–P75 по вакансиям со скором 50–100 · пороговый пул: {minSample}
      </div>

      {/* Static manual benchmark — right inset moved below on mobile */}
      <aside className="market-worth-benchmark market-worth-benchmark--inline">
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
      </aside>
    </>
  );
}

// ---------------------------------------------------------------------------
// Tab: Стек
// ---------------------------------------------------------------------------

function StackTab({ sa }) {
  if (!sa) return <div className="tab-empty">Нет данных</div>;
  const { top_tech, total_pool, vacancies_with_stack, small_sample, degraded_reason } = sa;
  return (
    <>
      <FreqBlockHeader title="Что просит рынок" n={vacancies_with_stack} pool={total_pool} />
      {small_sample && <div className="market-worth-warning">{degraded_reason}</div>}
      {top_tech.length === 0
        ? <div className="stack-block-empty">Стек пока не собран</div>
        : <FreqList items={top_tech} nameKey="tech" pctKey="pct" countKey="count" />
      }
    </>
  );
}

// ---------------------------------------------------------------------------
// Tab: Бенефиты
// ---------------------------------------------------------------------------

function BenefitsTab({ ba }) {
  if (!ba) return <div className="tab-empty">Нет данных</div>;
  const { top_benefits, total_pool, vacancies_with_benefits, small_sample, degraded_reason } = ba;
  return (
    <>
      <FreqBlockHeader title="Что предлагает рынок" n={vacancies_with_benefits} pool={total_pool} />
      {small_sample && <div className="market-worth-warning">{degraded_reason}</div>}
      {top_benefits.length === 0
        ? <div className="stack-block-empty">Бенефиты пока не собраны</div>
        : <FreqList items={top_benefits} nameKey="benefit" pctKey="pct" countKey="count" />
      }
    </>
  );
}

// ---------------------------------------------------------------------------
// Tab: Требования
// ---------------------------------------------------------------------------

function RequirementsTab({ ra }) {
  if (!ra) return <div className="tab-empty">Нет данных</div>;
  const {
    seniority_dist, remote_dist,
    relocation_count, relocation_pct,
    total_pool, vacancies_with_seniority,
    small_sample, degraded_reason,
  } = ra;

  return (
    <>
      {small_sample && <div className="market-worth-warning">{degraded_reason}</div>}

      {/* Relocation highlight — shown first, prominent */}
      <div className="req-relocation-card">
        <div className="req-relocation-val">{relocation_pct}%</div>
        <div className="req-relocation-label">
          вакансий с релокацией
          <span className="req-relocation-abs"> ({relocation_count} из {total_pool})</span>
        </div>
      </div>

      {/* Seniority */}
      <div className="req-section">
        <FreqBlockHeader
          title="Грейд"
          n={vacancies_with_seniority}
          pool={total_pool}
        />
        {seniority_dist.length === 0
          ? <div className="stack-block-empty">Данных о грейде нет</div>
          : <FreqList items={seniority_dist} nameKey="grade" pctKey="pct" countKey="count" />
        }
      </div>

      {/* Work format */}
      <div className="req-section">
        <FreqBlockHeader
          title="Формат работы"
          n={total_pool}
          pool={total_pool}
        />
        <FreqList items={remote_dist.filter(d => d.count > 0)} nameKey="label" pctKey="pct" countKey="count" />
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// Tab bar
// ---------------------------------------------------------------------------

const TABS = ["Зарплата", "Стек", "Бенефиты", "Требования"];

function TabBar({ active, onChange }) {
  return (
    <div className="mw-tabs" role="tablist">
      {TABS.map((tab) => (
        <button
          key={tab}
          role="tab"
          aria-selected={active === tab}
          className={`mw-tab${active === tab ? " mw-tab--active" : ""}`}
          onClick={() => onChange(tab)}
        >
          {tab}
        </button>
      ))}
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
  const [activeTab, setActiveTab] = useState("Зарплата");

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
        <h1>Анализ рынка</h1>

        {loading && !data && <div className="loading">Загрузка…</div>}
        {error && <div className="error">{error}</div>}

        {data && (
          <>
            <TabBar active={activeTab} onChange={setActiveTab} />

            <div className="mw-tab-panel">
              {activeTab === "Зарплата" && <SalaryTab data={data} />}
              {activeTab === "Стек" && <StackTab sa={data.stack_analytics} />}
              {activeTab === "Бенефиты" && <BenefitsTab ba={data.benefits_analytics} />}
              {activeTab === "Требования" && <RequirementsTab ra={data.requirements_analytics} />}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
