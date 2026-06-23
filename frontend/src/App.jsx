import React from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/Sidebar.jsx";
import PipelineView from "./views/PipelineView.jsx";
import BorderlineView from "./views/BorderlineView.jsx";
import CompetenciesView from "./views/CompetenciesView.jsx";
import { FiltersProvider } from "./state/FiltersContext.jsx";

// Top-level shell: a persistent left sidebar + the routed main area. React
// Router owns client-side routes; the FastAPI catch-all serves index.html for
// these paths on a hard refresh so deep links work.
export default function App() {
  return (
    <FiltersProvider>
      <div className="app">
        <Sidebar />
        <main className="main">
        <Routes>
          <Route path="/" element={<PipelineView />} />
          <Route path="/item/:id" element={<PipelineView />} />
          <Route path="/borderline" element={<BorderlineView />} />
          <Route path="/borderline/item/:id" element={<BorderlineView />} />
          <Route path="/competencies" element={<CompetenciesView />} />
          <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </FiltersProvider>
  );
}
