import React, { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api.js";
import { useFilters } from "../state/FiltersContext.jsx";
import { LANES, laneForStatus } from "../lib.js";
import PipelineTable from "../components/PipelineTable.jsx";
import DetailPanel from "../components/DetailPanel.jsx";
import AddByUrl from "../components/AddByUrl.jsx";

// All lanes (+ the filter-only "other" bucket) start COLLAPSED: with the
// funnel now split into 7 lanes, expanding everything by default buried the
// at-a-glance "how many where" view the overview strip above exists to give.
const ALL_COLLAPSED = Object.fromEntries(
  [...LANES.map((l) => l.key), "other"].map((key) => [key, true])
);

// MAIN pipeline view: an overview strip (counts per stage, click to jump)
// above a hybrid kanban+table. Rows from GET /api/pipeline are grouped into
// status LANES (Ожидают решения / Одобрено / Отправлено / Ответили / Собес /
// Оффер / Отклонено). Items in other states only appear if a filter surfaces
// them (handled by an extra "Прочее" group so they are never silently dropped
// from a filtered view). Clicking a row opens the DetailPanel; an action
// there refreshes the list.
export default function PipelineView() {
  const { filters } = useFilters();
  const navigate = useNavigate();
  const { id } = useParams();
  const selectedId = id != null ? Number(id) : null;

  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [collapsed, setCollapsed] = useState(ALL_COLLAPSED);
  const laneRefs = useRef({});

  const toggleLane = useCallback((key) => {
    setCollapsed((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  // Overview pill click: always EXPAND (never toggle-closed from here — that
  // would be surprising from a "jump to" control) and scroll the section
  // into view.
  const jumpToLane = useCallback((key) => {
    setCollapsed((prev) => ({ ...prev, [key]: false }));
    requestAnimationFrame(() => {
      laneRefs.current[key]?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }, []);

  const fetchList = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listPipeline(filters);
      setItems(data);
    } catch (e) {
      setError("Ошибка загрузки пайплайна");
    } finally {
      setLoading(false);
    }
  }, [filters]);

  // Refetch whenever a filter changes (fetchList depends on filters).
  useEffect(() => {
    fetchList();
  }, [fetchList]);

  // Patch a single row in place after an action returns the updated detail, then
  // refetch so lane membership reflects the new status accurately.
  const onUpdated = useCallback(
    (updated) => {
      setItems((prev) =>
        prev.map((it) =>
          it.id === updated.id ? { ...it, status: updated.status } : it
        )
      );
      fetchList();
    },
    [fetchList]
  );

  // After an add (fresh or duplicate): refetch the list so the new/existing card
  // is present, then open it.
  const onAdded = useCallback(
    (itemId) => {
      fetchList();
      if (itemId != null) navigate(`/item/${itemId}`);
    },
    [fetchList, navigate]
  );

  const grouped = groupByLane(items);

  return (
    <div className="view">
      <div className="view-list">
        <h1>Пайплайн</h1>
        <AddByUrl onAdded={onAdded} />
        {loading && <div className="loading">Загрузка…</div>}
        {error && <div className="error">{error}</div>}

        {/* Overview: every stage's count at a glance, without expanding
            anything. Click a pill to open + jump to that section. */}
        <div className="lane-overview">
          {LANES.map((lane) => (
            <button
              key={lane.key}
              type="button"
              className="lane-pill"
              onClick={() => jumpToLane(lane.key)}
            >
              <span className="lane-pill-title">{lane.title}</span>
              <span className="lane-pill-count">{grouped[lane.key].length}</span>
            </button>
          ))}
          {grouped.other.length > 0 && (
            <button
              type="button"
              className="lane-pill"
              onClick={() => jumpToLane("other")}
            >
              <span className="lane-pill-title">Прочее</span>
              <span className="lane-pill-count">{grouped.other.length}</span>
            </button>
          )}
        </div>

        {LANES.map((lane) => (
          <section
            key={lane.key}
            className="lane"
            ref={(el) => (laneRefs.current[lane.key] = el)}
          >
            <h2 className="lane-title" onClick={() => toggleLane(lane.key)}>
              <span className="lane-arrow">{collapsed[lane.key] ? "▸" : "▾"}</span>
              {lane.title}
              <span className="lane-count">{grouped[lane.key].length}</span>
            </h2>
            {!collapsed[lane.key] && (
              lane.key === "declined" ? (
                <DeclinedList
                  items={grouped[lane.key]}
                  selectedId={selectedId}
                  onSelect={(itemId) => navigate(`/item/${itemId}`)}
                />
              ) : (
                <PipelineTable
                  items={grouped[lane.key]}
                  selectedId={selectedId}
                  onSelect={(itemId) => navigate(`/item/${itemId}`)}
                />
              )
            )}
          </section>
        ))}

        {grouped.other.length > 0 && (
          <section
            className="lane"
            ref={(el) => (laneRefs.current.other = el)}
          >
            <h2 className="lane-title" onClick={() => toggleLane("other")}>
              <span className="lane-arrow">{collapsed.other ? "▸" : "▾"}</span>
              Прочее (по фильтру)
              <span className="lane-count">{grouped.other.length}</span>
            </h2>
            {!collapsed.other && (
              <PipelineTable
                items={grouped.other}
                selectedId={selectedId}
                onSelect={(itemId) => navigate(`/item/${itemId}`)}
              />
            )}
          </section>
        )}
      </div>

      {selectedId != null && (
        <DetailPanel
          itemId={selectedId}
          onClose={() => navigate("/")}
          onUpdated={onUpdated}
        />
      )}
    </div>
  );
}

function groupByLane(items) {
  const out = {
    surfaced: [],
    approved: [],
    sent: [],
    screening: [],
    interview: [],
    offer: [],
    declined: [],
    other: [],
  };
  for (const it of items) {
    const lane = laneForStatus(it.status);
    if (lane) out[lane].push(it);
    else out.other.push(it);
  }
  return out;
}

function DeclinedList({ items, selectedId, onSelect }) {
  if (!items || items.length === 0) {
    return <div className="empty">Нет позиций</div>;
  }
  return (
    <ul className="declined-list">
      {items.map((it) => (
        <li
          key={it.id}
          className={`declined-item${it.id === selectedId ? " row-selected" : ""}`}
          onClick={() => onSelect(it.id)}
        >
          <span className="declined-role">{it.role || "—"}</span>
          {it.company && <span className="declined-company">{it.company}</span>}
          {it.decline_reason && (
            <span className="declined-reason">{it.decline_reason}</span>
          )}
        </li>
      ))}
    </ul>
  );
}
