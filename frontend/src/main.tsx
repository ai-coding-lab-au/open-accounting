import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import App from "./App";
import "./index.css";
import { DEFAULT_THEME, isThemeId } from "./lib/themes";

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

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);
