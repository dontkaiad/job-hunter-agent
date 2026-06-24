import React from "react";
import ScoreDot from "./ScoreDot.jsx";
import { salaryText, dateText, stackText } from "../lib.js";

// A plain table of pipeline rows. NO action buttons in rows (actions live in
// the detail panel). Clicking a row selects it (onSelect(id)).
export default function PipelineTable({ items, selectedId, onSelect }) {
  if (!items || items.length === 0) {
    return <div className="empty">Нет позиций</div>;
  }
  // Wrapper enables horizontal table scroll on narrow (mobile) screens; on
  // desktop it is an inert block (overflow visible) so the table is unchanged.
  return (
    <div className="table-scroll">
      <table className="pipeline-table">
        <thead>
          <tr>
            <th>Score</th>
            <th>Вакансия</th>
            <th>Стек</th>
            <th>Зарплата</th>
            <th>Дата</th>
          </tr>
        </thead>
        <tbody>
          {items.map((it) => (
            <tr
              key={it.id}
              className={it.id === selectedId ? "row-selected" : ""}
              onClick={() => onSelect(it.id)}
            >
              <td>
                <ScoreDot score={it.score} />
              </td>
              <td className="cell-role">{it.role || "—"}</td>
              <td className="cell-stack" title={stackText(it.stack)}>
                {stackText(it.stack)}
              </td>
              <td>{salaryText(it.salary)}</td>
              <td>{dateText(it.published_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
