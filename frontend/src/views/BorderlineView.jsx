import React, { useEffect, useState, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api.js";
import PipelineTable from "../components/PipelineTable.jsx";
import DetailPanel from "../components/DetailPanel.jsx";

// Пограничные (50-59): a single flat list of sub-surface candidates loaded with
// a FIXED band GET /api/pipeline?min_score=50&max_score=60 (max is half-open in
// the API, so 60 excludes 60+). These sit below the surface threshold so they
// are not in the main lanes. Same table + detail panel + actions.
export default function BorderlineView() {
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
      const data = await api.listPipeline({ minScore: 50, maxScore: 60 });
      setItems(data);
    } catch (e) {
      setError("Ошибка загрузки");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchList();
  }, [fetchList]);

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

  return (
    <div className="view">
      <div className="view-list">
        <h1>Пограничные (50-59)</h1>
        {loading && <div className="loading">Загрузка…</div>}
        {error && <div className="error">{error}</div>}
        <PipelineTable
          items={items}
          selectedId={selectedId}
          onSelect={(itemId) => navigate(`/borderline/item/${itemId}`)}
        />
      </div>

      {selectedId != null && (
        <DetailPanel
          itemId={selectedId}
          onClose={() => navigate("/borderline")}
          onUpdated={onUpdated}
        />
      )}
    </div>
  );
}
