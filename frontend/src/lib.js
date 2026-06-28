// PURE display helpers — no I/O. Kept separate from the API client and the
// React components so they can be unit-tested directly.

import {
  SURFACED,
  APPROVED,
  RESEARCHED,
  DRAFTED,
  SENT,
  SCREENING,
  INTERVIEW,
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
  // The "sent" lane holds the whole ACTIVE post-send funnel so those items stay
  // visible+clickable in the kanban (where the funnel buttons live). Terminal
  // offer/closed drop out of the lanes, like rejected/skipped.
  if (status === SENT || status === SCREENING || status === INTERVIEW)
    return "sent";
  if (status === "declined") return "declined";
  return null;
}

// Ordered lane definitions for the kanban+table main view.
export const LANES = [
  { key: "surfaced", title: "Ожидают решения" },
  { key: "approved", title: "Одобрено" },
  { key: "sent", title: "Отправлено" },
  { key: "declined", title: "Отклонено" },
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
      return [
        { action: "draft", label: "Сгенерировать отклик" },
        { action: "decline", label: "Отклонить", needsReason: true },
      ];
    case DRAFTED:
      return [
        { action: "sent", label: "Отметить отправленным" },
        { action: "decline", label: "Отклонить", needsReason: true },
      ];
    // Post-send response funnel (mirrors states.py T13..T21). decline/close are
    // available at every funnel stage; offer only after an interview.
    case SENT:
      return [
        { action: "screening", label: "Ответили / скрининг" },
        { action: "decline", label: "Отказ" },
        { action: "close", label: "Закрыть" },
      ];
    case SCREENING:
      return [
        { action: "interview", label: "Собес" },
        { action: "decline", label: "Отказ" },
        { action: "close", label: "Закрыть" },
      ];
    case INTERVIEW:
      return [
        { action: "offer", label: "Оффер 🎉" },
        { action: "decline", label: "Отказ" },
        { action: "close", label: "Закрыть" },
      ];
    case "declined":
    case "rejected":
      return [{ action: "approve", label: "Вернуть в работу" }];
    default:
      return [];
  }
}
