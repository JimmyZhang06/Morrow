import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./VisualRefinement.css";
import "./ChineseTheme.css";
import "./ReadingLayout.css";
import "./PageExperience.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
