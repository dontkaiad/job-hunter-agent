import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// Компетенции: Kai's hand-curated skill inventory (config/competencies.*.yaml,
// see job_hunter/competencies.py) crossed against real market demand — the
// "частота в вакансиях за N дней" figure comes from the SAME extract pipeline
// that already pulls a `stack` list out of every harvested vacancy
// (job_hunter/stack_analytics.py), just windowed and matched here.
//
// Layout: one term = one collapsed row (term + a short freq badge, single
// line, never wraps) that expands on click to reveal the 30-sec explainer /
// resume line / source pointer below — a per-row <details>, the same native
// disclosure pattern DetailPanel.jsx already uses for the vacancy text /
// benefits blocks (.vacancy / .vacancy-summary). A 5-column table with long
// free text in every cell doesn't fit a browser width; this does.
//
// Content (the "делаю" column, i.e. what each entry actually says) is filled
// separately, project by project, straight from reading each project's code
// — this view only renders whatever config/competencies.local.yaml currently
// holds. Until that pass runs, it renders the generic example.yaml content.

const BUCKET_META = {
  core: { label: "Сильное ядро", hint: "готово к резюме" },
  growing: { label: "В развитии", hint: "в приоритете" },
  skip: { label: "Сознательно не качаю", hint: "" },
  glossary: { label: "Словарь", hint: "понимаю термин, не заявляю опыт" },
};
const BUCKET_ORDER = ["core", "growing", "skip", "glossary"];

// How many project chips show inline in the collapsed row before the rest
// collapse into a "+N" chip — keeps the summary line from wrapping/colliding
// with the freq badge as more projects get reviewed.
const MAX_INLINE_PROJECT_CHIPS = 3;

function CompetencyRow({ e, days }) {
  const hasFreq = e.market_count > 0;
  const hasBothTerms = e.term_ru && e.term_en;
  const evidence = e.evidence ?? [];
  const projects = [...new Set(evidence.map((ev) => ev.project).filter(Boolean))];
  const shownProjects = projects.slice(0, MAX_INLINE_PROJECT_CHIPS);
  const hiddenCount = projects.length - shownProjects.length;

  return (
    <details className="comp-row">
      <summary className="comp-row-summary">
        <span className="comp-row-term">
          {e.term_ru || e.term_en}
          {hasBothTerms && <span className="comp-row-term-en"> · {e.term_en}</span>}
        </span>
        {shownProjects.length > 0 && (
          <span className="comp-row-project-chips" title="реализовано в проектах">
            {shownProjects.map((p) => (
              <span key={p} className="comp-row-project-chip">{p}</span>
            ))}
            {hiddenCount > 0 && (
              <span className="comp-row-project-chip comp-row-project-chip--more">
                +{hiddenCount}
              </span>
            )}
          </span>
        )}
        <span className={`comp-row-freq${hasFreq ? "" : " comp-row-freq--zero"}`}>
          {e.market_pct}%
        </span>
      </summary>

      <div className="comp-row-body">
        {(e.explainer_ru || e.explainer_en) && (
          <p className="comp-row-explainer">{e.explainer_ru || e.explainer_en}</p>
        )}

        {e.bucket === "core" && e.resume_line && (
          <div className="comp-row-field">
            <span className="comp-row-field-label">Резюме</span>
            <span>{e.resume_line}</span>
          </div>
        )}

        <div className="comp-row-field comp-row-field--evidence">
          <span className="comp-row-field-label">Где реализовано</span>
          {evidence.length > 0 ? (
            <ul className="comp-evidence-list">
              {evidence.map((ev, i) => (
                <li key={`${ev.project}-${i}`}>
                  <span className="comp-evidence-project">{ev.project}</span>
                  <span className="comp-source-ref">{ev.source_ref}</span>
                  {ev.note && <span className="comp-evidence-note"> — {ev.note}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <span className="muted">не размечено</span>
          )}
        </div>

        <div className="comp-row-field">
          <span className="comp-row-field-label">Частота, {days}д</span>
          <span className={hasFreq ? "" : "muted"}>
            {e.market_pct}% {hasFreq && `· n=${e.market_count}`}
          </span>
        </div>
      </div>
    </details>
  );
}

function BucketBlock({ bucket, entries, days }) {
  const meta = BUCKET_META[bucket];
  return (
    <section className={`comp-bucket comp-bucket--${bucket}`}>
      <div className="comp-bucket-head">
        <span className="comp-bucket-dot" />
        <h2>{meta.label}</h2>
        {meta.hint && <span className="comp-bucket-hint">{meta.hint}</span>}
        <span className="comp-bucket-n">{entries.length}</span>
      </div>

      {entries.length === 0 ? (
        <div className="empty">Пока пусто</div>
      ) : (
        <>
          <div className="comp-list-head">
            <span className="comp-list-head-freq">{days}д</span>
          </div>
          <div className="comp-list">
            {entries.map((e, i) => (
              <CompetencyRow key={`${e.term_en || e.term_ru}-${i}`} e={e} days={days} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function GapCandidates({ items, days }) {
  if (!items || items.length === 0) return null;
  return (
    <section className="comp-gaps">
      <div className="comp-gaps-head">
        <span className="comp-gaps-badge">пробел</span>
        <h2>Просит рынок, но не размечено</h2>
      </div>
      <div className="mw-tile-bars">
        {items.map((g) => (
          <div key={g.term} className="mw-tile-bar">
            <span className="mw-tile-bar-label">{g.term}</span>
            <div className="mw-tile-bar-track">
              <div
                className="mw-tile-bar-fill mw-tile-bar-fill--amber"
                style={{ width: `${Math.min(100, g.market_pct)}%` }}
              />
            </div>
            <span className="mw-tile-bar-pct">{g.market_pct}%</span>
          </div>
        ))}
      </div>
      <div className="comp-gaps-note">
        встречаются в вакансиях за последние {days} дней, но ни один пункт
        компетенций на них не сматчился
      </div>
    </section>
  );
}

function matchesQuery(e, q) {
  if (!q) return true;
  const haystack = [
    e.term_ru,
    e.term_en,
    e.explainer_ru,
    e.explainer_en,
    e.resume_line,
    ...(e.evidence ?? []).flatMap((ev) => [ev.project, ev.source_ref, ev.note]),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(q);
}

export default function CompetenciesView() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [query, setQuery] = useState("");
  const [activeBucket, setActiveBucket] = useState(BUCKET_ORDER[0]);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.getCompetencies();
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

  const days = data?.market?.window_days ?? "?";
  const q = query.trim().toLowerCase();
  const byBucket = BUCKET_ORDER.map((bucket) => ({
    bucket,
    entries: (data?.entries ?? [])
      .filter((e) => e.bucket === bucket)
      .filter((e) => matchesQuery(e, q)),
  }));
  const totalMatched = byBucket.reduce((n, b) => n + b.entries.length, 0);
  const activeEntries = byBucket.find((b) => b.bucket === activeBucket)?.entries ?? [];

  return (
    <div className="view competencies-view">
      <div className="view-list">
        <h1>Компетенции</h1>

        {loading && !data && <div className="loading">Загрузка…</div>}
        {error && <div className="error">{error}</div>}

        {data && (
          <>
            {data.market?.small_sample && (
              <div className="market-worth-warning">
                {data.market.degraded_reason}
              </div>
            )}

            <div className="comp-search">
              <input
                type="text"
                className="comp-search-input"
                placeholder="Поиск: термин, проект, файл..."
                value={query}
                onChange={(ev) => setQuery(ev.target.value)}
              />
              {q && (
                <span className="comp-search-count">
                  {totalMatched} {totalMatched === 1 ? "совпадение" : "совпадений"}
                </span>
              )}
            </div>

            <div className="comp-tabs" role="tablist">
              {byBucket.map(({ bucket, entries }) => {
                const meta = BUCKET_META[bucket];
                const active = bucket === activeBucket;
                return (
                  <button
                    key={bucket}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    className={`comp-tab comp-bucket--${bucket}${active ? " comp-tab--active" : ""}`}
                    onClick={() => setActiveBucket(bucket)}
                  >
                    <span className="comp-tab-dot" />
                    {meta.label}
                    <span className="comp-tab-n">{entries.length}</span>
                  </button>
                );
              })}
            </div>

            <BucketBlock bucket={activeBucket} entries={activeEntries} days={days} />

            {!q && <GapCandidates items={data.gap_candidates} days={days} />}
          </>
        )}
      </div>
    </div>
  );
}
