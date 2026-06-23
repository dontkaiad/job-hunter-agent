import React, { createContext, useContext, useState, useCallback } from "react";

// Shared filter state for the pipeline list. Lives above both the Sidebar
// (which edits it) and the PipelineView (which reads it to build the
// /api/pipeline query). Tri-state fields (remote, processed) use the strings
// "any" | "true" | "false" so they map cleanly to the boolean query params.

export const DEFAULT_FILTERS = {
  status: "all",
  minScore: "",
  maxScore: "",
  remote: "any",
  processed: "any",
  q: "",
};

const FiltersContext = createContext(null);

export function FiltersProvider({ children }) {
  const [filters, setFilters] = useState(DEFAULT_FILTERS);

  const setFilter = useCallback((key, value) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
  }, []);

  const resetFilters = useCallback(() => setFilters(DEFAULT_FILTERS), []);

  return (
    <FiltersContext.Provider value={{ filters, setFilter, resetFilters }}>
      {children}
    </FiltersContext.Provider>
  );
}

export function useFilters() {
  const ctx = useContext(FiltersContext);
  if (!ctx) throw new Error("useFilters must be used within FiltersProvider");
  return ctx;
}
