import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import React from "react";
import Sidebar from "./components/Sidebar.jsx";
import { FiltersProvider } from "./state/FiltersContext.jsx";

// Smoke test: the sidebar renders the profile + nav without network. (The data
// views fetch from /api, so they are exercised by the pure-logic + backend
// tests rather than a full integration render here.)
describe("Sidebar smoke", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllEnvs();
  });

  it("renders profile and nav items", () => {
    // The profile name is build-time env (no name is baked into the repo): stub a generic one.
    vi.stubEnv("VITE_PROFILE_NAME", "Кандидат");
    render(
      <MemoryRouter>
        <FiltersProvider>
          <Sidebar />
        </FiltersProvider>
      </MemoryRouter>
    );
    expect(screen.getByText("Кандидат")).toBeDefined();
    expect(screen.getByText("Пайплайн")).toBeDefined();
    expect(screen.getByText("Пограничные (40-49)")).toBeDefined();
    expect(screen.getByText("Компетенции")).toBeDefined();
  });
});
