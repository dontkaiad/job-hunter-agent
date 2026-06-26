import React, { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import Filters from "./Filters.jsx";
import { useFilters } from "../state/FiltersContext.jsx";
import { api } from "../api.js";

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
// Format a salary range compactly for the sidebar (11px mono).
function _fmtSidebarRange(mn, mx, sym) {
  const f = (n) => n == null ? null : n.toLocaleString("ru-RU");
  if (mn != null && mx != null) return `${f(mn)}–${f(mx)} ${sym}`;
  if (mn != null) return `${f(mn)}+ ${sym}`;
  if (mx != null) return `до ${f(mx)} ${sym}`;
  return null;
}

export default function Sidebar() {
  const { filters, setFilter, resetFilters } = useFilters();
  const [mw, setMw] = useState(null);

  useEffect(() => {
    // Best-effort: silently ignore 404 (no cache yet) and errors.
    api.getMarketWorth().then(setMw).catch(() => {});
  }, []);

  // Profile name/role are env-driven (no real name baked into the repo). The
  // defaults are generic placeholders.
  const profileName = import.meta.env.VITE_PROFILE_NAME || "Кандидат";
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
            {mw && !mw.degraded && (mw.ru_min || mw.intl_min) ? (
              <>
                {mw.ru_min != null && (
                  <span className="profile-salary-line">
                    <span className="profile-salary-val">
                      {_fmtSidebarRange(mw.ru_min, mw.ru_max, "₽")}
                    </span>
                    {" "}РФ
                  </span>
                )}
                {mw.intl_min != null && (
                  <span className="profile-salary-line">
                    <span className="profile-salary-val">
                      {_fmtSidebarRange(
                        mw.intl_min, mw.intl_max,
                        mw.intl_currency === "USD" ? "$" : "€"
                      )}
                    </span>
                    {" "}интл
                  </span>
                )}
              </>
            ) : (
              "Анализ рынка →"
            )}
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
