import React, { useState } from "react";
import { api, ApiError } from "../api.js";

// "Добавить вакансию по ссылке": paste a vacancy URL -> POST /api/items/add ->
// the backend fetches it, runs the SAME pipeline (extract -> score -> advance),
// and it appears as a scored card. On success we hand the new id back to the
// parent (which refetches the list + navigates to the card). Duplicate and
// fetch-failure both surface inline — no card is created in those cases.
//
// onAdded(itemId, { duplicate }) is called for both a fresh add and a duplicate
// (so the parent can refresh + jump to the existing card).
export default function AddByUrl({ onAdded }) {
  const [url, setUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  async function submit(e) {
    e.preventDefault();
    const value = url.trim();
    if (!value || loading) return;

    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const res = await api.addByUrl(value);
      if (res.duplicate) {
        setNotice(`Уже в пайплайне (#${res.item_id})`);
      } else {
        setUrl("");
      }
      if (onAdded) onAdded(res.item_id, { duplicate: res.duplicate });
    } catch (e) {
      // 422 carries a friendly detail ("couldn't read that page" / invalid URL);
      // anything else is a generic failure.
      const msg =
        e instanceof ApiError && typeof e.message === "string" && e.message
          ? humanize(e.message)
          : "Не удалось добавить ссылку";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }

  return (
    <form className="add-url" onSubmit={submit}>
      <label className="add-url-label" htmlFor="add-url-input">
        Добавить вакансию по ссылке
      </label>
      <div className="add-url-row">
        <input
          id="add-url-input"
          className="add-url-input"
          type="url"
          inputMode="url"
          placeholder="https://…"
          value={url}
          onChange={(ev) => setUrl(ev.target.value)}
          disabled={loading}
          autoComplete="off"
        />
        <button
          className="add-url-btn"
          type="submit"
          disabled={loading || !url.trim()}
        >
          {loading ? "Читаю…" : "Добавить"}
        </button>
      </div>
      {error && <div className="add-url-error">{error}</div>}
      {notice && <div className="add-url-notice">{notice}</div>}
    </form>
  );
}

// Map the backend's English detail strings to the Russian UI voice.
function humanize(detail) {
  const d = detail.toLowerCase();
  if (d.includes("read")) return "Не удалось прочитать страницу по ссылке";
  if (d.includes("url")) return "Это не похоже на корректную ссылку";
  return detail;
}
