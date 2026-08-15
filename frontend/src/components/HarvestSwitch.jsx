import React, { useEffect, useState } from "react";
import { api } from "../api.js";

// Sidebar-resident toggle for the harvest pause switch (GET/POST
// /api/pipeline-status, see job_hunter/webapi.py). Paused = the daily
// scheduled harvest (and the manual one-shot) both no-op before ingest: no
// new vacancies fetched, nothing processed, nothing sent. Purely a dashboard
// control — job_hunter.run.harvest is the actual enforcement point.

// changed_at is stored/served in UTC (matches the rest of the API), but this
// label is "you just did this" status, not a coarse publish date — showing it
// by UTC CALENDAR DAY (lib.js's dateText, used for vacancy published_at) reads
// as flat-out wrong near local midnight in any timezone ahead of UTC (e.g.
// toggling at 00:30 local reads as "yesterday"). Render in the browser's own
// local time, with minutes, so the moment is unambiguous.
function localSince(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString(undefined, {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function HarvestSwitch() {
  const [status, setStatus] = useState(null); // { paused, changed_at } | null while loading
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getPipelineStatus()
      .then((s) => {
        if (!cancelled) setStatus(s);
      })
      .catch(() => {
        if (!cancelled) setError("не удалось загрузить статус");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggle() {
    if (!status || pending) return;
    setPending(true);
    setError(null);
    const next = !status.paused;
    try {
      const updated = await api.setPipelineStatus(next);
      setStatus(updated);
    } catch {
      setError("не удалось изменить статус");
    } finally {
      setPending(false);
    }
  }

  if (error && !status) {
    return <div className="harvest-switch harvest-switch-error">{error}</div>;
  }
  if (!status) {
    return <div className="harvest-switch harvest-switch-loading">…</div>;
  }

  const { paused, changed_at } = status;

  return (
    <div className={`harvest-switch ${paused ? "is-paused" : "is-active"}`}>
      <button
        type="button"
        role="switch"
        aria-checked={!paused}
        aria-label={paused ? "Включить поиск вакансий" : "Приостановить поиск вакансий"}
        className="harvest-switch-toggle"
        onClick={toggle}
        disabled={pending}
      >
        <span className="harvest-switch-knob" />
      </button>
      <div className="harvest-switch-text">
        <span className="harvest-switch-label">
          {paused ? "Поиск на паузе" : "Поиск активен"}
        </span>
        <span className="harvest-switch-since">
          {changed_at ? `с ${localSince(changed_at)}` : "не переключался"}
        </span>
      </div>
      {error && <span className="harvest-switch-inline-error">{error}</span>}
    </div>
  );
}
