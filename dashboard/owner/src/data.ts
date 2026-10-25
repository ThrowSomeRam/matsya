import type { Evidence, ObligationsPayload, OwnerView } from "./types";

// The API first; the precomputed demo payload (public/demo, copied from eval/demo) when it is not running.
async function getJson<T>(apiPath: string, fallback: string): Promise<{ data: T; source: "api" | "demo file" }> {
  try {
    const r = await fetch(`/api${apiPath}`, { headers: { Accept: "application/json" } });
    if (r.ok && (r.headers.get("content-type") ?? "").includes("json")) return { data: (await r.json()) as T, source: "api" };
  } catch {
    // API not reachable: fall through to the demo file
  }
  const r = await fetch(fallback);
  if (!r.ok) throw new Error(`no data: API down and ${fallback} missing`);
  return { data: (await r.json()) as T, source: "demo file" };
}

export function tenantId(): string {
  return new URLSearchParams(location.search).get("tenant") ?? "demo";
}

export const loadOwnerView = () =>
  getJson<OwnerView>(`/tenants/${encodeURIComponent(tenantId())}/owner-view`, "/demo/owner_view.json");

export const loadEvidence = () => getJson<Evidence>("/evidence/naive-vs-ours", "/demo/naive_vs_ours.json");

// Optional: the page works without it (the obligations panels are then hidden).
export const loadObligations = () =>
  getJson<ObligationsPayload>(`/tenants/${encodeURIComponent(tenantId())}/obligations`, "/demo/obligations.json")
    .then((r) => r.data).catch(() => null);
