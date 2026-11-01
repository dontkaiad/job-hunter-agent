import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { REJECT_CHIPS } from "../lib.js";

// BLIND labelling of the golden set (/label). One card at a time: no score, no
// rationale, no source. «Подошла бы» / «Не уверена» save at once and move on;
// «Нет» opens the optional reason chips and saves on «Дальше» (Enter / →).
// Keys: 1 подошла · 2 нет · 3 не уверена · 4..9 chips (after «нет») · Enter/→ дальше · ← назад.
export default function LabelView() {
  const [card, setCard] = useState(null);
  const [progress, setProgress] = useState(null);
  const [label, setLabel] = useState(null);
  const [chips, setChips] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [finished, setFinished] = useState(false);
  const history = useRef([]); // sample ids already shown, for «назад»

  const show = useCallback((c) => {
    setCard(c);
    setLabel(c ? c.label : null);
    setChips(c ? c.chips || [] : []);
  }, []);

  const loadNext = useCallback(async () => {
    setError(null);
    try {
      const r = await api.goldenNext();
      setProgress(r.progress);
      if (!r.card) {
        setFinished(true);
        show(null);
      } else {
        setFinished(false);
        show(r.card);
      }
    } catch {
      setError("Не удалось загрузить карточку");
    }
  }, [show]);

  useEffect(() => { loadNext(); }, [loadNext]);

  const save = useCallback(async (lbl, chs) => {
    if (!card || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.goldenLabel(card.sample_id, { label: lbl, chips: lbl === "no" ? chs : [] });
      setProgress(r.progress);
      history.current.push(card.sample_id);
      // after a «назад» edit, continue with the next UNlabelled card
      await loadNext();
    } catch {
      setError("Не удалось сохранить, попробуйте ещё раз");
    } finally {
      setBusy(false);
    }
  }, [card, busy, loadNext]);

  const choose = useCallback((lbl) => {
    if (!card || busy) return;
    if (lbl === "no") { setLabel("no"); return; }
    setLabel(lbl);
    save(lbl, []);
  }, [card, busy, save]);

  const toggleChip = useCallback((code) => {
    setChips((cur) => (cur.includes(code) ? cur.filter((c) => c !== code) : [...cur, code]));
  }, []);

  const back = useCallback(async () => {
    const prev = history.current.pop();
    if (prev == null) return;
    try {
      const r = await api.goldenCard(prev);
      setProgress(r.progress);
      setFinished(false);
      show(r.card);
    } catch {
      setError("Не удалось открыть предыдущую карточку");
    }
  }, [show]);

  useEffect(() => {
    function onKey(e) {
      if (e.target && /^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
      if (e.key === "1") choose("yes");
      else if (e.key === "2") choose("no");
      else if (e.key === "3") choose("unsure");
      else if (label === "no" && /^[4-9]$/.test(e.key)) toggleChip(REJECT_CHIPS[Number(e.key) - 4].code);
      else if ((e.key === "Enter" || e.key === "ArrowRight") && label === "no") save("no", chips);
      else if (e.key === "ArrowLeft") back();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [choose, toggleChip, save, back, label, chips]);

  const wave = progress && progress.waves.find((w) => w.wave === (card ? card.wave : progress.current_wave));

  return (
    <div className="label-page">
      <h2>Разметка</h2>
      {progress && wave && (
        <div className="muted">
          Волна {wave.wave}: размечено {wave.done} из {wave.total}
          {card && <> · карточка {card.position}/{card.total}</>}
        </div>
      )}
      {error && <div className="error">{error}</div>}
      {finished && (
        <div className="notice">
          {progress && progress.finished
            ? "Все волны размечены. Спасибо!"
            : "Нет неразмеченных карточек в этой волне."}
        </div>
      )}
      {card && (
        <div className="label-card">
          <h3>{card.title || "(без названия)"}</h3>
          <div className="label-meta">
            {card.company && <span>{card.company}</span>}
            {card.seniority && <span>{card.seniority}</span>}
            {card.location && <span>{card.location}</span>}
            {card.remote != null && <span>удалёнка: {card.remote ? "да" : "нет"}</span>}
            {card.relocation != null && <span>релокация: {card.relocation ? "да" : "нет"}</span>}
            {card.salary && <span>{card.salary}</span>}
          </div>
          {card.stack && card.stack.length > 0 && <div className="label-stack">Стек: {card.stack.join(", ")}</div>}
          <p className="label-snippet">{card.snippet}</p>
          {card.link && (
            <a href={card.link} target="_blank" rel="noreferrer noopener">Открыть оригинал</a>
          )}
          <div className="label-buttons" role="group" aria-label="Подходит ли вакансия">
            {[["yes", "✅ Подошла бы", "1"], ["no", "❌ Нет", "2"], ["unsure", "🤔 Не уверена", "3"]].map(([k, text, hk]) => (
              <button
                key={k}
                type="button"
                className={"btn btn-action" + (label === k ? " active" : "")}
                aria-pressed={label === k}
                data-label={k}
                disabled={busy}
                onClick={() => choose(k)}
              >
                {text} <kbd>{hk}</kbd>
              </button>
            ))}
          </div>
          {label === "no" && (
            <div className="label-no">
              <div className="reason-chips" role="group" aria-label="Причины (необязательно)">
                {REJECT_CHIPS.map((c, i) => (
                  <button
                    key={c.code}
                    type="button"
                    className={"chip" + (chips.includes(c.code) ? " chip-on" : "")}
                    aria-pressed={chips.includes(c.code)}
                    data-chip={c.code}
                    disabled={busy}
                    onClick={() => toggleChip(c.code)}
                  >
                    {c.label} <kbd>{i + 4}</kbd>
                  </button>
                ))}
              </div>
              <button type="button" className="btn btn-action" disabled={busy} onClick={() => save("no", chips)}>
                Дальше →
              </button>
            </div>
          )}
          <div className="label-nav">
            <button type="button" className="btn" onClick={back} disabled={busy || history.current.length === 0}>
              ← назад
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
