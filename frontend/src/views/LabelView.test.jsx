import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";

vi.mock("../api.js", () => ({
  api: { goldenNext: vi.fn(), goldenCard: vi.fn(), goldenLabel: vi.fn() },
}));
import { api } from "../api.js";
import LabelView from "./LabelView.jsx";

const progress = (done) => ({ waves: [{ wave: 1, total: 3, done }], current_wave: 1, finished: false });
const card = (id, pos, over = {}) => ({
  sample_id: id, position: pos, total: 3, wave: 1, title: `Role ${id}`, company: "Acme", seniority: "senior",
  stack: ["python"], location: "Berlin", remote: true, relocation: null, salary: null,
  snippet: "Build LLM things", link: "https://acme.io/jobs/1", label: null, chips: [], ...over,
});

beforeEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("LabelView (blind labelling)", () => {
  it("shows the card and nothing about score, rationale or source", async () => {
    api.goldenNext.mockResolvedValue({ card: card(1, 1), progress: progress(0) });
    const { container } = render(<LabelView />);
    await screen.findByText("Role 1");
    expect(screen.getByText(/Волна 1: размечено 0 из 3/)).toBeDefined();
    const text = container.textContent.toLowerCase();
    for (const leak of ["score", "обоснование", "канал", "source"]) expect(text).not.toContain(leak);
  });

  it("«Подошла бы» saves at once and moves to the next card", async () => {
    api.goldenNext
      .mockResolvedValueOnce({ card: card(1, 1), progress: progress(0) })
      .mockResolvedValueOnce({ card: card(2, 2), progress: progress(1) });
    api.goldenLabel.mockResolvedValue({ ok: true, progress: progress(1) });
    render(<LabelView />);
    await screen.findByText("Role 1");
    fireEvent.click(screen.getByText(/Подошла бы/));
    await screen.findByText("Role 2");
    expect(api.goldenLabel).toHaveBeenCalledWith(1, { label: "yes", chips: [] });
  });

  it("«Нет» waits for «Дальше» and sends the chosen reason chips", async () => {
    api.goldenNext
      .mockResolvedValueOnce({ card: card(1, 1), progress: progress(0) })
      .mockResolvedValueOnce({ card: card(2, 2), progress: progress(1) });
    api.goldenLabel.mockResolvedValue({ ok: true, progress: progress(1) });
    render(<LabelView />);
    await screen.findByText("Role 1");
    fireEvent.click(screen.getByText(/^❌ Нет/));
    expect(api.goldenLabel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText(/не мой профиль\/стек/));
    fireEvent.click(screen.getByText(/seniority/));
    fireEvent.click(screen.getByText(/Дальше/));
    await screen.findByText("Role 2");
    expect(api.goldenLabel).toHaveBeenCalledWith(1, { label: "no", chips: ["fit", "seniority"] });
  });

  it("keyboard: 3 = не уверена, ← goes back to the previous card with its label", async () => {
    api.goldenNext
      .mockResolvedValueOnce({ card: card(1, 1), progress: progress(0) })
      .mockResolvedValueOnce({ card: card(2, 2), progress: progress(1) });
    api.goldenLabel.mockResolvedValue({ ok: true, progress: progress(1) });
    api.goldenCard.mockResolvedValue({ card: card(1, 1, { label: "unsure" }), progress: progress(1) });
    render(<LabelView />);
    await screen.findByText("Role 1");
    fireEvent.keyDown(window, { key: "3" });
    await screen.findByText("Role 2");
    expect(api.goldenLabel).toHaveBeenCalledWith(1, { label: "unsure", chips: [] });
    fireEvent.keyDown(window, { key: "ArrowLeft" });
    await screen.findByText("Role 1");
    await waitFor(() => expect(screen.getByText(/Не уверена/).closest("button").getAttribute("aria-pressed")).toBe("true"));
  });

  it("says so when everything is labelled", async () => {
    api.goldenNext.mockResolvedValue({ card: null, progress: { waves: [{ wave: 1, total: 3, done: 3 }], current_wave: null, finished: true } });
    render(<LabelView />);
    await screen.findByText(/Все волны размечены/);
  });
});
