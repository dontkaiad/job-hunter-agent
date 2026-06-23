import React, { useEffect, useState, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api.js";
import { useFilters } from "../state/FiltersContext.jsx";
import { LANES, laneForStatus } from "../lib.js";
import PipelineTable from "../components/PipelineTable.jsx";
import DetailPanel from "../components/DetailPanel.jsx";

// MAIN pipeline view: a hybrid kanban+table. Rows from GET /api/pipeline are
// grouped into status LANES (Ожидают решения / Одобрено / Отправлено). Items in
// other states only appear if a filter surfaces them (handled by an extra
// "Прочее" group so they are never silently dropped from a filtered view).
// Clicking a row opens the DetailPanel; an action there refreshes the list.
export default function PipelineView() {
  const { filters } = useFilters();
  const navigate = useNavigate();
  const { id } = useParams();
  const selectedId = id != null ? Number(id) : null;

  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

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

  const grouped = groupByLane(items);

  return (
    <div className="view">
      <div className="view-list">
        <h1>Пайплайн</h1>
        {loading && <div className="loading">Загрузка…</div>}
        {error && <div className="error">{error}</div>}

        {LANES.map((lane) => (
          <section key={lane.key} className="lane">
            <h2 className="lane-title">
              {lane.title} <span className="lane-count">{grouped[lane.key].length}</span>
            </h2>
            <PipelineTable
              items={grouped[lane.key]}
              selectedId={selectedId}
              onSelect={(itemId) => navigate(`/item/${itemId}`)}
            />
          </section>
        ))}

        {grouped.other.length > 0 && (
          <section className="lane">
            <h2 className="lane-title">
              Прочее (по фильтру){" "}
              <span className="lane-count">{grouped.other.length}</span>
            </h2>
            <PipelineTable
              items={grouped.other}
              selectedId={selectedId}
              onSelect={(itemId) => navigate(`/item/${itemId}`)}
            />
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
  const out = { surfaced: [], approved: [], sent: [], other: [] };
  for (const it of items) {
    const lane = laneForStatus(it.status);
    if (lane) out[lane].push(it);
    else out.other.push(it);
  }
  return out;
}
