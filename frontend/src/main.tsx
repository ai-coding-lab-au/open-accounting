import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";

import App from "./App";
import "./index.css";
import { DEFAULT_THEME, isThemeId } from "./lib/themes";
import { createLocalQueryClient } from "./lib/queryClient";

// Apply persisted theme before first paint so there's no flash of default.
try {
  const raw = localStorage.getItem("accounting.theme");
  if (raw) {
    const parsed = JSON.parse(raw);
    const id = parsed?.state?.theme;
    if (id && isThemeId(id)) {
      document.documentElement.setAttribute("data-theme", id);
    } else {
      document.documentElement.setAttribute("data-theme", DEFAULT_THEME);
    }
  } else {
    document.documentElement.setAttribute("data-theme", DEFAULT_THEME);
  }
} catch {
  document.documentElement.setAttribute("data-theme", DEFAULT_THEME);
}

const globalQueryClient = createLocalQueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={globalQueryClient}>
      <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
