import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App.jsx";
import { applyFavicon } from "./favicon.js";
import "./styles.css";

// Set the tab favicon from the gitignored local image when present (no-op on a
// fresh clone -> browser default). Same out-of-repo mechanism as the avatar.
applyFavicon();

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>
);
