/**
 * Публичная конфигурация из .env (VITE_*). В бандл попадает только то, что
 * перечислено здесь; секретов среди этих значений быть не может.
 */
export type ApiMode = "http" | "mock";

export interface AppConfig {
  apiBaseUrl: string;
  apiMode: ApiMode;
  debugToggle: boolean;
}

function readBool(value: string | undefined, fallback: boolean): boolean {
  if (value === undefined || value === "") return fallback;
  return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
}

export function loadConfig(env: Record<string, string | undefined> = import.meta.env, search = ""): AppConfig {
  const params = new URLSearchParams(search);
  const modeParam = params.get("mock") === "1" ? "mock" : undefined;
  const mode = (modeParam ?? env.VITE_API_MODE ?? "http") === "mock" ? "mock" : "http";
  return {
    apiBaseUrl: (env.VITE_API_BASE_URL ?? "").replace(/\/+$/, ""),
    apiMode: mode,
    debugToggle: params.get("debug") === "1" || readBool(env.VITE_DEBUG_TOGGLE, false),
  };
}
