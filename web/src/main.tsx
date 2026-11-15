import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createHttpApi } from "./api/httpAdapter";
import { createMockApi } from "./api/mockAdapter";
import { App } from "./App";
import { loadConfig } from "./config";
import "./styles/tokens.css";

const config = loadConfig(import.meta.env, window.location.search);
const api = config.apiMode === "mock" ? createMockApi() : createHttpApi(config.apiBaseUrl);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App api={api} config={config} />
  </StrictMode>,
);
