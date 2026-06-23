// PURE display helpers — no I/O. Kept separate from the API client and the
// React components so they can be unit-tested directly.

import {
  SURFACED,
  APPROVED,
  RESEARCHED,
  DRAFTED,
  SENT,
} from "./states.js";

// Score band classification. The 50-59 BORDERLINE band is deliberately a
// distinct colour (orange) from the 60-74 amber/yellow band so sub-surface
// candidates read differently at a glance.
//   green      >= 75
//   amber      60-74
//   borderline 50-59  (distinct orange)
//   red        < 50
//   none       null/undefined score
export function scoreBand(score) {
  if (score == null || Number.isNaN(score)) return "none";
  if (score >= 75) return "green";
  if (score >= 60) return "amber";
  if (score >= 50) return "borderline";
  return "red";
}

// Salary cell text: prefer the server's bot-card display string; else compose
// from min/max + currency; else em dash.
export function salaryText(salary) {
  if (!salary) return "—";
  if (salary.display) return salary.display;
  const { min, max, currency } = salary;
  const cur = currency ? ` ${currency}` : "";
  if (min != null && max != null) return `${min}–${max}${cur}`;
  if (max != null) return `${max}${cur}`;
  if (min != null) return `${min}${cur}`;
  return "—";
}

// Compact date (YYYY-MM-DD) from an ISO timestamp; em dash when absent/bad.
export function dateText(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toISOString().slice(0, 10);
}

export function stackText(stack) {
  if (!stack || !stack.length) return "—";
  return stack.join(", ");
}

// Which lane an item belongs to based on its status. Items in any other state
// (rejected/skipped/backlog/closed/discovered/...) return null and are not
// shown in the lanes unless a filter surfaces them in a flat list.
export function laneForStatus(status) {
  if (status === SURFACED) return "surfaced";
  if (status === APPROVED || status === RESEARCHED || status === DRAFTED)
    return "approved";
  if (status === SENT) return "sent";
  return null;
}

// Ordered lane definitions for the kanban+table main view.
export const LANES = [
  { key: "surfaced", title: "Ожидают решения" },
  { key: "approved", title: "Одобрено" },
  { key: "sent", title: "Отправлено" },
];

// The per-state action buttons shown INSIDE the detail panel. Each entry is
// {action, label}; action maps 1:1 to a POST endpoint.
export function actionsForStatus(status) {
  switch (status) {
    case SURFACED:
      return [
        { action: "approve", label: "Одобрить" },
        { action: "backlog", label: "В бэклог" },
        { action: "skip", label: "Пропустить" },
      ];
    case "backlog":
      return [
        { action: "approve", label: "Одобрить" },
        { action: "skip", label: "Пропустить" },
      ];
    case APPROVED:
    case RESEARCHED:
      return [{ action: "draft", label: "Сгенерировать отклик" }];
    case DRAFTED:
      return [{ action: "sent", label: "Отметить отправленным" }];
    default:
      return [];
  }
}
