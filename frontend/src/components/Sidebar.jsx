import React from "react";
import { NavLink } from "react-router-dom";
import Filters from "./Filters.jsx";
import { useFilters } from "../state/FiltersContext.jsx";

// Avatar is an OPTIONAL local-only asset. import.meta.glob tolerates ZERO
// matches at build time, so a fresh clone / the Docker build (which has no
// avatar file) still builds — `avatar` is then null and we render the initials
// placeholder. The maintainer's machine has avatar.local.png, so it shows.
const _avatars = import.meta.glob("../assets/avatar.local.*", {
  eager: true,
  query: "?url",
  import: "default",
});
const avatar = Object.values(_avatars)[0] ?? null;

// LEFT SIDEBAR: profile block, nav, and the pipeline filters. The filters live
// in a shared context so the PipelineView refetches when they change. The
// Filters block is only meaningful on the main pipeline view; on the borderline
// / competencies pages it still renders but those views ignore it (borderline
// uses a fixed min/max band, competencies is a stub).

export default function Sidebar() {
  const { filters, setFilter, resetFilters } = useFilters();

  // Profile name/role are env-driven (no real name baked into the repo). The
  // default fallback is used when VITE_PROFILE_NAME is not set at build time.
  const profileName = import.meta.env.VITE_PROFILE_NAME || "";
  const profileRole = import.meta.env.VITE_PROFILE_ROLE || "AI/LLM Engineer";
  const initials = profileName
    .split(/\s+/)
    .filter(Boolean)
    .map((w) => w[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();

  return (
    <aside className="sidebar">
      <section className="profile">
        <div className="profile-avatar">
          {avatar ? (
            <img
              className="profile-avatar-img"
              src={avatar}
              alt={profileName}
            />
          ) : (
            <div className="profile-avatar-fallback" aria-hidden="true">
              {initials}
            </div>
          )}
        </div>
        <div className="profile-id">
          <div className="profile-name">{profileName}</div>
          <div className="profile-role">{profileRole}</div>
          <NavLink to="/market-worth" className="profile-salary">
            <span className="profile-salary-line">
              <span className="profile-salary-val">200–300к</span> ₽
            </span>
            <span className="profile-salary-line">
              <span className="profile-salary-val">$4.5–8k</span> интл
            </span>
          </NavLink>
        </div>
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
        <NavLink to="/market-worth" className="nav-link">
          Анализ рынка
        </NavLink>
      </nav>

      <Filters
        filters={filters}
        setFilter={setFilter}
        resetFilters={resetFilters}
      />
    </aside>
  );
}
