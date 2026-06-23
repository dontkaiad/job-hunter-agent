import React, { useEffect, useState, useCallback } from "react";
import { api } from "../api.js";
import { actionsForStatus, salaryText } from "../lib.js";

// Side panel showing one item's full detail (GET /api/items/:id) plus the
// per-state action buttons (POST /api/items/:id/<action>). On a successful
// action it updates itself with the returned ItemDetail and calls onUpdated so
// the parent list can refresh the changed row. 409 -> a brief inline notice;
// 401 is handled in the api client (-> /login).
export default function DetailPanel({ itemId, onClose, onUpdated }) {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async (id) => {
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const d = await api.getItem(id);
      setDetail(d);
    } catch (e) {
      setError(e.status === 404 ? "Позиция не найдена" : "Ошибка загрузки");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (itemId != null) load(itemId);
  }, [itemId, load]);

  async function runAction(action) {
    setBusy(true);
    setNotice(null);
    try {
      const updated = await api.act(itemId, action);
      setDetail(updated);
      if (onUpdated) onUpdated(updated);
    } catch (e) {
      if (e.status === 409) {
        setNotice("Действие недоступно для текущего статуса");
      } else if (e.status === 404) {
        setNotice("Позиция не найдена");
      } else {
        setNotice("Ошибка выполнения действия");
      }
    } finally {
      setBusy(false);
    }
  }

  if (itemId == null) return null;

  return (
    <aside className="detail-panel">
      <div className="detail-head">
        <h2>Позиция #{itemId}</h2>
        <button type="button" className="btn" onClick={onClose}>
          Закрыть
        </button>
      </div>

      {loading && <div className="loading">Загрузка…</div>}
      {error && <div className="error">{error}</div>}

      {detail && (
        <div className="detail-body">
          <div className="detail-status">
            Статус: <strong>{detail.status}</strong>
            {detail.score != null && <span> · score {detail.score}</span>}
          </div>

          <DetailField label="Вакансия" value={detail.title} />
          <DetailField label="Компания" value={detail.company} />
          <DetailField
            label="Стек"
            value={detail.stack && detail.stack.join(", ")}
          />
          <DetailField label="Грейд" value={detail.seniority} />
          <DetailField label="Зарплата" value={salaryText(detail.salary)} />
          <DetailField
            label="Удалёнка"
            value={fmtBool(detail.remote)}
          />
          <DetailField label="Релокация" value={fmtBool(detail.relocation)} />
          <DetailField label="Локация" value={detail.location} />
          <DetailField
            label="Контакт"
            value={detail.contact}
          />
          {detail.source && detail.source.link && (
            <div className="detail-field">
              <span className="detail-label">Источник</span>
              <a href={detail.source.link} target="_blank" rel="noreferrer">
                {detail.source.channel || detail.source.link}
              </a>
            </div>
          )}

          <Actions
            status={detail.status}
            busy={busy}
            notice={notice}
            onAction={runAction}
          />

          <ListBlock label="Причины" items={detail.reasons} />
          <ListBlock label="Бенефиты" items={detail.benefits} />

          <TextBlock label="Обоснование" value={detail.reasoning} />
          <TextBlock label="Отклик (draft)" value={detail.draft} />

          <Research research={detail.research} />

          <History history={detail.history} />
        </div>
      )}
    </aside>
  );
}

function fmtBool(v) {
  if (v == null) return null;
  return v ? "да" : "нет";
}

function DetailField({ label, value }) {
  if (!value) return null;
  return (
    <div className="detail-field">
      <span className="detail-label">{label}</span>
      <span>{value}</span>
    </div>
  );
}

function Actions({ status, busy, notice, onAction }) {
  const actions = actionsForStatus(status);
  return (
    <div className="actions">
      {actions.length === 0 && (
        <div className="muted">Нет доступных действий для статуса «{status}»</div>
      )}
      {actions.map((a) => (
        <button
          key={a.action}
          type="button"
          className="btn btn-action"
          data-action={a.action}
          disabled={busy}
          onClick={() => onAction(a.action)}
        >
          {a.label}
        </button>
      ))}
      {notice && <div className="notice">{notice}</div>}
    </div>
  );
}

function ListBlock({ label, items }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="block">
      <h4>{label}</h4>
      <ul>
        {items.map((x, i) => (
          <li key={i}>{x}</li>
        ))}
      </ul>
    </div>
  );
}

function TextBlock({ label, value }) {
  if (!value) return null;
  return (
    <div className="block">
      <h4>{label}</h4>
      <pre className="text-block">{value}</pre>
    </div>
  );
}

// research may be a dict (with summary / sourced_facts) or a string or null.
function Research({ research }) {
  if (!research) return null;
  if (typeof research === "string") {
    return <TextBlock label="Research" value={research} />;
  }
  const summary = research.summary;
  const facts = research.sourced_facts || research.facts;
  return (
    <div className="block">
      <h4>Research</h4>
      {summary && <p>{summary}</p>}
      {Array.isArray(facts) && facts.length > 0 && (
        <ul>
          {facts.map((f, i) => (
            <li key={i}>{typeof f === "string" ? f : JSON.stringify(f)}</li>
          ))}
        </ul>
      )}
      {!summary && !facts && (
        <pre className="text-block">{JSON.stringify(research, null, 2)}</pre>
      )}
    </div>
  );
}

function History({ history }) {
  if (!history || history.length === 0) return null;
  return (
    <div className="block">
      <h4>История</h4>
      <ul className="history">
        {history.map((t, i) => (
          <li key={i}>
            <span className="hist-states">
              {t.from_state || "∅"} → {t.to_state || "∅"}
            </span>
            <span className="hist-meta">
              {t.kind} · {t.actor}
              {t.created_at ? ` · ${t.created_at}` : ""}
            </span>
            {t.reason && <span className="hist-reason"> — {t.reason}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}
