import React, { useEffect, useState, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api.js";
import PipelineTable from "../components/PipelineTable.jsx";
import DetailPanel from "../components/DetailPanel.jsx";

// Отрезано по гео: vacancies the geo guard rejected although their score cleared the
// threshold. Nothing is hidden: each row shows the rule's reason (geo_reason), the card
// opens in the same detail panel, and rows expire 30 days after the cut.
export default function GeoCutView() {
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
      const data = await api.listPipeline({ geo: "cut" });
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
        <h1>Отрезано по гео (30 дней)</h1>
        {loading && <div className="loading">Загрузка…</div>}
        {error && <div className="error">{error}</div>}
        {items.length > 0 && (
          <ul className="geo-reasons">
            {items.map((it) => (
              <li key={it.id}>
                #{it.id} {it.role || "—"} — {it.geo_reason || "—"}
              </li>
            ))}
          </ul>
        )}
        <PipelineTable
          items={items}
          selectedId={selectedId}
          onSelect={(itemId) => navigate(`/geo-cut/item/${itemId}`)}
        />
      </div>

      {selectedId != null && (
        <DetailPanel
          itemId={selectedId}
          onClose={() => navigate("/geo-cut")}
          onUpdated={onUpdated}
        />
      )}
    </div>
  );
}
