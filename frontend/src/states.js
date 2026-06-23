// The 12 pipeline states, mirrored from job_hunter/states.py. Used for the
// status filter select and lane/action mapping. Kept in sync MANUALLY with the
// backend — these are stable constants.

export const DISCOVERED = "discovered";
export const EXTRACTED = "extracted";
export const SCORED = "scored";
export const REJECTED = "rejected";
export const SURFACED = "surfaced";
export const SKIPPED = "skipped";
export const BACKLOG = "backlog";
export const APPROVED = "approved";
export const RESEARCHED = "researched";
export const DRAFTED = "drafted";
export const SENT = "sent";
export const CLOSED = "closed";

// Russian labels for the status filter select.
export const STATE_LABELS = {
  discovered: "discovered",
  extracted: "extracted",
  scored: "scored",
  rejected: "rejected",
  surfaced: "surfaced (ожидают)",
  skipped: "skipped",
  backlog: "backlog",
  approved: "approved",
  researched: "researched",
  drafted: "drafted",
  sent: "sent",
  closed: "closed",
};

export const ALL_STATES = [
  DISCOVERED,
  EXTRACTED,
  SCORED,
  REJECTED,
  SURFACED,
  SKIPPED,
  BACKLOG,
  APPROVED,
  RESEARCHED,
  DRAFTED,
  SENT,
  CLOSED,
];
