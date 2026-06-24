// Thin client over the REAL dashboard API (job_hunter/webapi.py). It invents NO
// endpoints — only the documented routes are called:
//   GET  /api/pipeline?status&min_score&max_score&remote&processed&q
//   GET  /api/items/:id
//   POST /api/items/:id/{approve|backlog|skip|sent|draft}
//
// AUTH: the SPA assumes an existing .heylark.dev Telegram-login session cookie
// (hl_session). Cookies are same-origin so they ride along automatically with
// credentials: "same-origin". On ANY 401 the server-rendered /login page owns
// the flow, so we hard-redirect there.

const JSON_HEADERS = { Accept: "application/json" };

class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

async function request(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: JSON_HEADERS,
    ...options,
  });

  // 401 -> not authed (or session expired): the server renders /login.
  if (res.status === 401) {
    window.location.href = "/login";
    // Throw so callers stop; the redirect is already in flight.
    throw new ApiError("authentication required", 401, null);
  }

  let body = null;
  const text = await res.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!res.ok) {
    const detail =
      (body && typeof body === "object" && body.detail) || res.statusText;
    throw new ApiError(detail || `HTTP ${res.status}`, res.status, body);
  }
  return body;
}

// Build a /api/pipeline query string from a filter object, omitting empty /
// "any" values so the server applies only the active filters.
function pipelineQuery(filters = {}) {
  const params = new URLSearchParams();
  const { status, minScore, maxScore, remote, processed, q } = filters;
  if (status && status !== "all") params.set("status", status);
  if (minScore !== "" && minScore != null) params.set("min_score", minScore);
  if (maxScore !== "" && maxScore != null) params.set("max_score", maxScore);
  if (remote === "true" || remote === "false") params.set("remote", remote);
  if (processed === "true" || processed === "false")
    params.set("processed", processed);
  if (q && q.trim()) params.set("q", q.trim());
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export { pipelineQuery };

export const api = {
  ApiError,
  pipelineQuery,

  listPipeline(filters) {
    return request(`/api/pipeline${pipelineQuery(filters)}`);
  },

  getItem(id) {
    return request(`/api/items/${id}`);
  },

  // The 5 action endpoints. Each returns the updated ItemDetail. action is one
  // of: approve | backlog | skip | sent | draft.
  act(id, action) {
    return request(`/api/items/${id}/${action}`, { method: "POST" });
  },

  // Add a vacancy by URL. Returns { item_id, state, score, duplicate }.
  // duplicate=true => the URL is already in the pipeline (no new card created).
  // A 422 ApiError is thrown for an invalid or unreadable URL (detail carries
  // the human message).
  addByUrl(url) {
    return request(`/api/items/add`, {
      method: "POST",
      headers: { ...JSON_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
  },
};

export { ApiError };
