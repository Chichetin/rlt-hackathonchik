import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { installStaticApi, STATIC_DEMO } from "./staticApi";
import "./styles.css";
import "./glass.css";
import "./search-compose.css";
import "./responsive-ui.css";
import "./results-view.css";
import "./interior-ui.css";
import "./supplier-results.css";
import "./work-glass.css";
import "./work-typography.css";

if (STATIC_DEMO) installStaticApi(); // the static stand without the API (#283)

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
