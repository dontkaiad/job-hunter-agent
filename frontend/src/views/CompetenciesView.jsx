import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";

// Компетенции: Kai's hand-curated skill inventory (config/competencies.*.yaml,
// see job_hunter/competencies.py) crossed against real market demand — the
// "частота в вакансиях за N дней" column comes from the SAME extract pipeline
// that already pulls a `stack` list out of every harvested vacancy
// (job_hunter/stack_analytics.py), just windowed and matched here.
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

function EntryRow({ e }) {
  const hasFreq = e.market_count > 0;
  return (
    <tr>
      <td className="cell-role">
        <div>{e.term_ru || e.term_en}</div>
        {e.term_ru && e.term_en && (
          <div className="comp-term-en">{e.term_en}</div>
        )}
      </td>
      <td className="comp-explainer" title={e.explainer_en || undefined}>
        {e.explainer_ru || e.explainer_en || "—"}
      </td>
      {e.bucket === "core" ? (
        <td className="comp-resume">{e.resume_line || "—"}</td>
      ) : (
        <td className="comp-resume muted">—</td>
      )}
      <td className="comp-source">
        {e.source_ref ? (
          <>
            <div className="comp-source-ref">{e.source_ref}</div>
            {e.project && <div className="comp-source-project">{e.project}</div>}
          </>
        ) : (
          <span className="muted">не размечено</span>
        )}
      </td>
      <td className={`comp-freq${hasFreq ? "" : " comp-freq--zero"}`}>
        {hasFreq ? (
          <>
            <span className="comp-freq-pct">{e.market_pct}%</span>
            <span className="comp-freq-n">n={e.market_count}</span>
          </>
        ) : (
          "0%"
        )}
      </td>
    </tr>
  );
}

function BucketTable({ bucket, entries, days }) {
  const meta = BUCKET_META[bucket];
  return (
    <section className="comp-bucket">
      <div className="comp-bucket-head">
        <h2>{meta.label}</h2>
        {meta.hint && <span className="comp-bucket-hint">{meta.hint}</span>}
        <span className="comp-bucket-n">{entries.length}</span>
      </div>
      {entries.length === 0 ? (
        <div className="empty">Пока пусто</div>
      ) : (
        <div className="table-scroll">
          <table className="pipeline-table comp-table">
            <thead>
              <tr>
                <th>Термин</th>
                <th>30 сек</th>
                <th>Резюме</th>
                <th>Источник</th>
                <th>Частота, {days}д</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e, i) => (
                <EntryRow key={`${e.term_en || e.term_ru}-${i}`} e={e} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function GapCandidates({ items, days }) {
  if (!items || items.length === 0) return null;
  return (
    <section className="comp-bucket comp-gaps">
      <div className="comp-bucket-head">
        <h2>Просит рынок, но не размечено</h2>
        <span className="comp-bucket-hint">кандидаты на пробел</span>
      </div>
      <div className="mw-tile-bars">
        {items.map((g) => (
          <div key={g.term} className="mw-tile-bar">
            <span className="mw-tile-bar-label">{g.term}</span>
            <div className="mw-tile-bar-track">
              <div
                className="mw-tile-bar-fill"
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

export default function CompetenciesView() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

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
  const byBucket = BUCKET_ORDER.map((bucket) => ({
    bucket,
    entries: (data?.entries ?? []).filter((e) => e.bucket === bucket),
  }));

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

            {byBucket.map(({ bucket, entries }) => (
              <BucketTable
                key={bucket}
                bucket={bucket}
                entries={entries}
                days={days}
              />
            ))}

            <GapCandidates items={data.gap_candidates} days={days} />
          </>
        )}
      </div>
    </div>
  );
}
