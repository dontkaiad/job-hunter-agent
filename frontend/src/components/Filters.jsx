import React from "react";
import { ALL_STATES, STATE_LABELS } from "../states.js";

// Filter controls driving GET /api/pipeline. Each change updates the shared
// filter context; the PipelineView reacts and refetches. Tri-state selects use
// "any" to mean "omit the param".
export default function Filters({ filters, setFilter, resetFilters }) {
  return (
    <section className="filters">
      <h3 className="filters-title">Фильтры</h3>

      <label className="field">
        <span>Поиск (q)</span>
        <input
          type="text"
          value={filters.q}
          placeholder="компания / роль / стек"
          onChange={(e) => setFilter("q", e.target.value)}
        />
      </label>

      <label className="field">
        <span>Статус</span>
        <select
          value={filters.status}
          onChange={(e) => setFilter("status", e.target.value)}
        >
          <option value="all">все</option>
          {ALL_STATES.map((s) => (
            <option key={s} value={s}>
              {STATE_LABELS[s] || s}
            </option>
          ))}
        </select>
      </label>

      <div className="field-row">
        <label className="field">
          <span>Score min</span>
          <input
            type="number"
            value={filters.minScore}
            onChange={(e) => setFilter("minScore", e.target.value)}
          />
        </label>
        <label className="field">
          <span>Score max</span>
          <input
            type="number"
            value={filters.maxScore}
            onChange={(e) => setFilter("maxScore", e.target.value)}
          />
        </label>
      </div>

      <label className="field">
        <span>Remote</span>
        <select
          value={filters.remote}
          onChange={(e) => setFilter("remote", e.target.value)}
        >
          <option value="any">любой</option>
          <option value="true">да</option>
          <option value="false">нет</option>
        </select>
      </label>

      <label className="field">
        <span>Разобрано</span>
        <select
          value={filters.processed}
          onChange={(e) => setFilter("processed", e.target.value)}
        >
          <option value="any">любые</option>
          <option value="true">разобрано</option>
          <option value="false">неразобрано</option>
        </select>
      </label>

      <button type="button" className="btn" onClick={resetFilters}>
        Сбросить фильтры
      </button>
    </section>
  );
}
