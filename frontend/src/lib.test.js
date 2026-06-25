import { describe, it, expect, vi } from "vitest";
import {
  scoreBand,
  salaryText,
  dateText,
  stackText,
  laneForStatus,
  actionsForStatus,
  LANES,
} from "./lib.js";
import { pipelineQuery, api, ApiError } from "./api.js";

describe("scoreBand", () => {
  it("classifies the four bands with a distinct borderline", () => {
    expect(scoreBand(90)).toBe("green");
    expect(scoreBand(75)).toBe("green");
    expect(scoreBand(74)).toBe("amber");
    expect(scoreBand(60)).toBe("amber");
    expect(scoreBand(59)).toBe("borderline");
    expect(scoreBand(50)).toBe("borderline");
    expect(scoreBand(49)).toBe("red");
    expect(scoreBand(null)).toBe("none");
    expect(scoreBand(undefined)).toBe("none");
  });
});

describe("salaryText", () => {
  it("prefers display, falls back to min-max, else em dash", () => {
    expect(salaryText({ display: "~290k ₽" })).toBe("~290k ₽");
    expect(salaryText({ min: 200, max: 300, currency: "RUB" })).toBe(
      "200–300 RUB"
    );
    expect(salaryText({ max: 300, currency: "USD" })).toBe("300 USD");
    expect(salaryText({})).toBe("—");
    expect(salaryText(null)).toBe("—");
  });
});

describe("dateText / stackText", () => {
  it("formats date and stack", () => {
    expect(dateText("2026-06-20T10:00:00Z")).toBe("2026-06-20");
    expect(dateText(null)).toBe("—");
    expect(stackText(["python", "fastapi"])).toBe("python, fastapi");
    expect(stackText([])).toBe("—");
  });
});

describe("laneForStatus", () => {
  it("maps statuses to the three lanes, null otherwise", () => {
    expect(laneForStatus("surfaced")).toBe("surfaced");
    expect(laneForStatus("approved")).toBe("approved");
    expect(laneForStatus("researched")).toBe("approved");
    expect(laneForStatus("drafted")).toBe("approved");
    expect(laneForStatus("sent")).toBe("sent");
    // Active post-send funnel rides the "sent" lane; terminals drop out.
    expect(laneForStatus("screening")).toBe("sent");
    expect(laneForStatus("interview")).toBe("sent");
    expect(laneForStatus("offer")).toBe(null);
    expect(laneForStatus("declined")).toBe(null);
    expect(laneForStatus("rejected")).toBe(null);
    expect(laneForStatus("backlog")).toBe(null);
  });
});

describe("actionsForStatus", () => {
  it("returns per-state actions", () => {
    expect(actionsForStatus("surfaced").map((a) => a.action)).toEqual([
      "approve",
      "backlog",
      "skip",
    ]);
    expect(actionsForStatus("backlog").map((a) => a.action)).toEqual([
      "approve",
      "skip",
    ]);
    expect(actionsForStatus("approved").map((a) => a.action)).toEqual([
      "draft",
    ]);
    expect(actionsForStatus("researched").map((a) => a.action)).toEqual([
      "draft",
    ]);
    expect(actionsForStatus("drafted").map((a) => a.action)).toEqual(["sent"]);
    // Post-send funnel (mirrors states.py T13..T21).
    expect(actionsForStatus("sent").map((a) => a.action)).toEqual([
      "screening",
      "decline",
      "close",
    ]);
    expect(actionsForStatus("screening").map((a) => a.action)).toEqual([
      "interview",
      "decline",
      "close",
    ]);
    expect(actionsForStatus("interview").map((a) => a.action)).toEqual([
      "offer",
      "decline",
      "close",
    ]);
  });
});

describe("pipelineQuery", () => {
  it("omits any/empty values and serializes active filters", () => {
    expect(pipelineQuery({})).toBe("");
    expect(
      pipelineQuery({
        status: "surfaced",
        minScore: 50,
        maxScore: 60,
        remote: "true",
        processed: "false",
        q: "  python  ",
      })
    ).toBe(
      "?status=surfaced&min_score=50&max_score=60&remote=true&processed=false&q=python"
    );
    expect(pipelineQuery({ status: "all", remote: "any", q: "   " })).toBe("");
  });

  it("borderline view params: min_score=50&max_score=60 (half-open)", () => {
    // The BorderlineView calls api.listPipeline({ minScore: 50, maxScore: 60 }).
    // max_score=60 is half-open (score < 60), so score=60 is excluded. This is
    // the correct band for 50–59 distinct from the 60–74 amber.
    expect(pipelineQuery({ minScore: 50, maxScore: 60 })).toBe(
      "?min_score=50&max_score=60"
    );
  });

  it("minScore=0 is included (falsy-but-valid value)", () => {
    // 0 is a valid score (not empty/null), so it must NOT be omitted.
    expect(pipelineQuery({ minScore: 0 })).toBe("?min_score=0");
  });

  it("empty q omitted; whitespace-only q omitted", () => {
    expect(pipelineQuery({ q: "" })).toBe("");
    expect(pipelineQuery({ q: "   " })).toBe("");
  });
});

// ---------------------------------------------------------------------------
// TESTER-ADDED: missing coverage.
// ---------------------------------------------------------------------------

describe("scoreBand edge cases", () => {
  it("score=0 is red (not none)", () => {
    expect(scoreBand(0)).toBe("red");
  });

  it("score=100 is green", () => {
    expect(scoreBand(100)).toBe("green");
  });

  it("NaN is none", () => {
    expect(scoreBand(NaN)).toBe("none");
  });

  it("50-59 is DISTINCT from 60-74 (the whole point of the borderline band)", () => {
    const band50 = scoreBand(50);
    const band59 = scoreBand(59);
    const band60 = scoreBand(60);
    const band74 = scoreBand(74);
    // All borderline values must share one label, all amber another, and the two must differ.
    expect(band50).toBe("borderline");
    expect(band59).toBe("borderline");
    expect(band60).toBe("amber");
    expect(band74).toBe("amber");
    expect(band50).not.toBe(band60);
  });
});

describe("LANES titles", () => {
  it("has exactly 3 lanes with correct Russian titles", () => {
    expect(LANES).toHaveLength(3);
    expect(LANES[0]).toEqual({ key: "surfaced", title: "Ожидают решения" });
    expect(LANES[1]).toEqual({ key: "approved", title: "Одобрено" });
    expect(LANES[2]).toEqual({ key: "sent", title: "Отправлено" });
  });
});

describe("actionsForStatus — match backend transitions", () => {
  it("sent state offers the post-send funnel (screening/decline/close)", () => {
    // The old deterministic T13 sent->closed is gone; SENT now has manual exits.
    expect(actionsForStatus("sent").map((a) => a.action)).toEqual([
      "screening",
      "decline",
      "close",
    ]);
  });

  it("rejected/skipped/closed/offer/declined return empty (terminal states)", () => {
    expect(actionsForStatus("rejected")).toEqual([]);
    expect(actionsForStatus("skipped")).toEqual([]);
    expect(actionsForStatus("closed")).toEqual([]);
    expect(actionsForStatus("offer")).toEqual([]);
    expect(actionsForStatus("declined")).toEqual([]);
  });

  it("offer is only reachable after interview (not from sent/screening)", () => {
    expect(actionsForStatus("sent").map((a) => a.action)).not.toContain("offer");
    expect(actionsForStatus("screening").map((a) => a.action)).not.toContain(
      "offer",
    );
    expect(actionsForStatus("interview").map((a) => a.action)).toContain("offer");
  });

  it("drafted returns only [sent] (T12 drafted->sent is the sole HITL from drafted)", () => {
    const actions = actionsForStatus("drafted").map((a) => a.action);
    expect(actions).toEqual(["sent"]);
  });

  it("backlog has no 'backlog' action (cannot self-loop)", () => {
    const actions = actionsForStatus("backlog").map((a) => a.action);
    expect(actions).not.toContain("backlog");
  });

  it("surfaced does NOT offer 'draft' directly (must approve first)", () => {
    // The backend T11/T10 chain only runs from approved/researched.
    // Offering 'draft' from surfaced would cause a 409 on the backend.
    const actions = actionsForStatus("surfaced").map((a) => a.action);
    expect(actions).not.toContain("draft");
    expect(actions).not.toContain("sent");
  });
});

describe("api.js 401 redirect handler", () => {
  it("redirects to /login on a 401 response", async () => {
    // Mock fetch to return a 401.
    const mockLocation = { href: "" };
    const originalLocation = Object.getOwnPropertyDescriptor(
      globalThis,
      "location"
    );
    Object.defineProperty(globalThis, "location", {
      value: mockLocation,
      writable: true,
      configurable: true,
    });
    global.fetch = vi.fn().mockResolvedValue({
      status: 401,
      ok: false,
      text: async () => "",
    });

    // The call should throw (to stop callers) and set window.location.href.
    await expect(api.listPipeline({})).rejects.toThrow();
    expect(mockLocation.href).toBe("/login");

    // Restore.
    if (originalLocation) {
      Object.defineProperty(globalThis, "location", originalLocation);
    }
    vi.restoreAllMocks();
  });
});

describe("api.js 409 does NOT redirect (surfaces to caller)", () => {
  it("throws ApiError with status 409, does not redirect to /login", async () => {
    const mockHref = { href: "" };
    const orig = Object.getOwnPropertyDescriptor(globalThis, "location");
    Object.defineProperty(globalThis, "location", {
      value: mockHref,
      writable: true,
      configurable: true,
    });
    global.fetch = vi.fn().mockResolvedValue({
      status: 409,
      ok: false,
      statusText: "Conflict",
      text: async () => JSON.stringify({ detail: "cannot approve from sent" }),
    });

    await expect(api.act(1, "approve")).rejects.toMatchObject({ status: 409 });
    // Must NOT redirect.
    expect(mockHref.href).toBe("");

    if (orig) Object.defineProperty(globalThis, "location", orig);
    vi.restoreAllMocks();
  });
});
