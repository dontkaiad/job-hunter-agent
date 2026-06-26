import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// Format a ₽ / € / $ range for display.
function fmtRange(min, max, currency) {
  const sym = { RUB: "₽", EUR: "€", USD: "$" }[currency] ?? currency;
  const fmt = (n) =>
    n == null ? null : n.toLocaleString("ru-RU");
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

export default function MarketWorthView() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
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

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      const result = await api.refreshMarketWorth();
      setData(result);
    } catch (e) {
      setError(e.message || "Ошибка обновления");
    } finally {
      setRefreshing(false);
    }
  }, []);

  const minSample = data?.min_sample ?? 3;

  return (
    <div className="view market-worth-view">
      <div className="view-list">
        <div className="market-worth-header">
          <h1>Анализ рынка</h1>
          <button
            className={`btn-refresh${refreshing ? " loading" : ""}`}
            onClick={handleRefresh}
            disabled={refreshing || loading}
          >
            {refreshing ? "Пересчитываю…" : "Пересчитать"}
          </button>
        </div>

        {/* Static manual-research benchmark — hardcoded, no API */}
        <div className="market-worth-benchmark">
          <div className="market-worth-benchmark-title">Ориентир по рынку</div>
          <div className="market-worth-benchmark-subtitle">
            applied AI / LLM engineer · middle · ресёрч 06.2026
          </div>
          <div className="market-worth-benchmark-rows">
            <div className="market-worth-benchmark-row">
              <span className="market-worth-benchmark-flag">🇷🇺</span>
              <div>
                <div className="market-worth-benchmark-range">200 000 – 300 000 ₽/мес</div>
                <div className="market-worth-benchmark-note">
                  Не соглашаться ниже 200к. Целиться в 250–300к — стек выше базового middle.
                </div>
              </div>
            </div>
            <div className="market-worth-benchmark-row">
              <span className="market-worth-benchmark-flag">🌍</span>
              <div>
                <div className="market-worth-benchmark-range">$4 500 – 8 000 /мес · €4 000 – 7 000</div>
                <div className="market-worth-benchmark-note">
                  Реально метить в $5–7k. Топ-бэнды ($200k+) требуют fine-tuning/PyTorch — не твой путь сейчас.
                </div>
              </div>
            </div>
          </div>
          <div className="market-worth-benchmark-sources">
            Источники: enigmai.ru, vc.ru/ai, hirehi.ru, ayautomate.com, kore1.com, remotelytalents.com, lemon.io
          </div>
          <div className="market-worth-benchmark-footer">
            Анализ актуален на 26.06.2026 · рекомендуется повторный анализ через LLM раз в 2–3 месяца
          </div>
        </div>

        {(loading || refreshing) && !data && (
          <div className="loading">Загрузка…</div>
        )}

        {error && <div className="error">{error}</div>}

        {data && (
          <div className="market-worth-body">
            {/* Pipeline pool stats */}
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
                <span className="market-worth-stat-label">с зарплатой интл</span>
              </div>
            </div>

            {/* Degraded warning */}
            {data.degraded && (
              <div className="market-worth-warning">
                ⚠️ {data.degraded_reason}
              </div>
            )}

            {/* Salary ranges */}
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
          </div>
        )}
      </div>
    </div>
  );
}
