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
  afterEach(cleanup);

  it("renders profile and nav items", () => {
    render(
      <MemoryRouter>
        <FiltersProvider>
          <Sidebar />
        </FiltersProvider>
      </MemoryRouter>
    );
    expect(screen.getByText("Кандидат")).toBeDefined();
    expect(screen.getByText("Пайплайн")).toBeDefined();
    expect(screen.getByText("Пограничные (50-59)")).toBeDefined();
    expect(screen.getByText("Компетенции")).toBeDefined();
  });
});
