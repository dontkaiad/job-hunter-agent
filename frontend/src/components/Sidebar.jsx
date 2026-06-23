import React from "react";
import { NavLink } from "react-router-dom";
import Filters from "./Filters.jsx";
import { useFilters } from "../state/FiltersContext.jsx";

// LEFT SIDEBAR: profile block, nav, and the pipeline filters. The filters live
// in a shared context so the PipelineView refetches when they change. The
// Filters block is only meaningful on the main pipeline view; on the borderline
// / competencies pages it still renders but those views ignore it (borderline
// uses a fixed min/max band, competencies is a stub).
export default function Sidebar() {
  const { filters, setFilter, resetFilters } = useFilters();

  return (
    <aside className="sidebar">
      <section className="profile">
        <div className="profile-name">Карина Ларк</div>
        <div className="profile-role">AI/LLM Engineer</div>
        {/* PLACEHOLDER: market-salary widget is a later pass. */}
        <div className="profile-salary">Зп по рынку: — (скоро)</div>
      </section>

      <nav className="nav">
        <NavLink to="/" end className="nav-link">
          Пайплайн
        </NavLink>
        <NavLink to="/borderline" className="nav-link">
          Пограничные (50-59)
        </NavLink>
        <NavLink to="/competencies" className="nav-link">
          Компетенции
        </NavLink>
        {/* DISABLED: market analysis is not built yet. */}
        <span className="nav-link nav-disabled" aria-disabled="true">
          Анализ рынка <em>(скоро)</em>
        </span>
      </nav>

      <Filters
        filters={filters}
        setFilter={setFilter}
        resetFilters={resetFilters}
      />
    </aside>
  );
}
