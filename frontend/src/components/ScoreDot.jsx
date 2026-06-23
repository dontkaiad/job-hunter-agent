import React from "react";
import { scoreBand } from "../lib.js";

// A colored band dot + the numeric score. Colour comes from scoreBand():
// green >=75, amber 60-74, borderline (orange) 50-59, red <50.
export default function ScoreDot({ score }) {
  const band = scoreBand(score);
  return (
    <span className="score-cell">
      <span className={`score-dot score-${band}`} title={band} />
      <span className="score-num">{score == null ? "—" : score}</span>
    </span>
  );
}
