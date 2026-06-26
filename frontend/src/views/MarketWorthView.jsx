import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// Format a ₽ / € / $ range for display.
function fmtRange(min, max, currency) {
  const sym = { RUB: "₽", EUR: "€", USD: "$" }[currency] ?? currency;
  const fmt = (n) =>
    n == null ? null : n.toLocaleString("ru-RU");
  if (min != null && max != null) return `${fmt(min)}–${fmt(max)} ${sym}/мес`;
  if (min != null) return `от ${fmt(min)} ${sym}/мес (потолок не найден)`;
  if (max != null) return `до ${fmt(max)} ${sym}/мес`;
  return "нет данных";
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
      if (e.status === 404) {
        setData(null); // no cache yet — show prompt
      } else {
        setError(e.message || "Ошибка загрузки");
      }
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
            {refreshing ? "Обновляю…" : "Обновить"}
          </button>
        </div>

        {(loading || refreshing) && !data && (
          <div className="loading">
            {refreshing
              ? "Ищу данные по рынку (10–30 сек)…"
              : "Загрузка…"}
          </div>
        )}

        {error && <div className="error">{error}</div>}

        {!loading && !data && !error && (
          <div className="market-worth-empty">
            <p>Данные ещё не собирались.</p>
            <p>
              Нажмите <strong>Обновить</strong> для первого поиска (~30 сек).
            </p>
          </div>
        )}

        {data && (
          <div className="market-worth-body">
            {(data.stale || data.degraded) && (
              <div className="market-worth-warning">
                {data.degraded && (
                  <span>⚠️ {data.degraded_reason}</span>
                )}
                {data.stale && !data.degraded && (
                  <span>Данные устарели — рекомендуется обновить</span>
                )}
              </div>
            )}

            <div className="market-worth-grid">
              <div className="market-worth-card">
                <div className="market-worth-label">🇷🇺 Россия</div>
                <div className="market-worth-range">
                  {fmtRange(data.ru_min, data.ru_max, data.ru_currency)}
                </div>
              </div>
              <div className="market-worth-card">
                <div className="market-worth-label">🌍 Международный</div>
                <div className="market-worth-range">
                  {fmtRange(data.intl_min, data.intl_max, data.intl_currency)}
                </div>
              </div>
            </div>

            {data.reasoning_short && (
              <p className="market-worth-reasoning">{data.reasoning_short}</p>
            )}

            {data.sources && data.sources.length > 0 && (
              <div className="market-worth-sources">
                <div className="market-worth-sources-title">Источники</div>
                <ul>
                  {data.sources.map((s, i) => (
                    <li key={i}>
                      {s.startsWith("http") ? (
                        <a href={s} target="_blank" rel="noreferrer">
                          {s}
                        </a>
                      ) : (
                        s
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="market-worth-meta">
              {data.age_days === 0
                ? "Обновлено сегодня"
                : `Обновлено ${data.age_days} д. назад`}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
