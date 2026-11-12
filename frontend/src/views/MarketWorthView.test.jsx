import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

vi.mock("../api.js", () => ({
  api: { getMarketWorth: vi.fn(), getProfileCard: vi.fn() },
}));
import { api } from "../api.js";
import MarketWorthView from "./MarketWorthView.jsx";

const market = {
  ru_min: null, ru_max: null, ru_currency: "RUB", intl_min: null, intl_max: null, intl_currency: "EUR",
  ru_sample_size: 0, intl_sample_size: 0, min_sample: 5, total_relevant_vacancies: 0,
  degraded: true, degraded_reason: "мало данных",
};

beforeEach(() => {
  cleanup();
  vi.resetAllMocks();
  api.getMarketWorth.mockResolvedValue(market);
});

describe("MarketWorthView benchmark block", () => {
  it("is hidden when the profile configures no benchmarks", async () => {
    api.getProfileCard.mockResolvedValue({ benchmarks: [], subtitle: "", sources: [] });
    render(<MarketWorthView />);
    await screen.findByText("Анализ рынка");
    await waitFor(() => expect(api.getProfileCard).toHaveBeenCalled());
    expect(screen.queryByText("Ориентир по рынку")).toBeNull();
  });

  it("is hidden when the profile card cannot be loaded", async () => {
    api.getProfileCard.mockRejectedValue(new Error("boom"));
    render(<MarketWorthView />);
    await screen.findByText("Анализ рынка");
    await waitFor(() => expect(api.getProfileCard).toHaveBeenCalled());
    expect(screen.queryByText("Ориентир по рынку")).toBeNull();
  });

  it("renders label, value, note, caption and sources from the profile only", async () => {
    api.getProfileCard.mockResolvedValue({
      benchmarks: [{ label: "A", value: "VALUE_ONE", note: "NOTE_ONE" }, { label: "B", value: "VALUE_TWO", note: "" }],
      subtitle: "CAPTION_X", sources: ["src.example"],
    });
    render(<MarketWorthView />);
    await screen.findByText("Ориентир по рынку");
    for (const t of ["VALUE_ONE", "NOTE_ONE", "VALUE_TWO", "CAPTION_X", "src.example"]) {
      expect(screen.getByText(t)).toBeDefined();
    }
  });
});

describe("MarketWorthView international pool caption", () => {
  const withIntl = (over) => ({
    ...market, degraded: false, degraded_reason: null, intl_min: 7000, intl_median: 12000, intl_max: 15000,
    intl_sample_size: 326, ...over,
  });

  it("says what the international figure is made of when the geo guard never judged the vacancies", async () => {
    api.getProfileCard.mockResolvedValue({ benchmarks: [], subtitle: "", sources: [] });
    api.getMarketWorth.mockResolvedValue(withIntl({
      intl_geo_unchecked: 325, intl_usd_share: 0.834, intl_top_source: "hn_hiring", intl_top_source_share: 0.506,
    }));
    render(<MarketWorthView />);
    const note = await screen.findByTestId("mw-intl-pool");
    expect(note.textContent).toContain("326 вакансий");
    expect(note.textContent).toContain("83% в USD");
    expect(note.textContent).toContain("HN Hiring 51%");
    expect(note.textContent).toContain("не проверена у 325");
    expect(note.textContent).toContain("ограничением по стране");
  });

  it("shows no caption when every pool vacancy was geo-checked or the pool is empty", async () => {
    api.getProfileCard.mockResolvedValue({ benchmarks: [], subtitle: "", sources: [] });
    api.getMarketWorth.mockResolvedValue(withIntl({ intl_geo_unchecked: 0 }));
    render(<MarketWorthView />);
    await screen.findByText("Анализ рынка");
    expect(screen.queryByTestId("mw-intl-pool")).toBeNull();
    cleanup();
    api.getMarketWorth.mockResolvedValue({ ...market });
    render(<MarketWorthView />);
    await screen.findByText("Анализ рынка");
    expect(screen.queryByTestId("mw-intl-pool")).toBeNull();
  });
});
