export const API_URL = (import.meta.env.VITE_API_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");

async function parseResponse(response) {
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const message = payload?.error?.message || payload?.detail || `Request failed (${response.status}).`;
    throw new Error(message);
  }
  return payload;
}

export async function checkReadiness() {
  const response = await fetch(`${API_URL}/ready`, { signal: AbortSignal.timeout(8000) });
  const payload = await response.json().catch(() => null);
  return {
    ready: response.ok && payload?.status === "ready",
    status: payload?.status || "unavailable",
    checks: payload?.checks || {},
  };
}

export async function getVoices() {
  const response = await fetch(`${API_URL}/api/v1/voices`, { signal: AbortSignal.timeout(10000) });
  const payload = await parseResponse(response);
  return payload?.voices || [];
}

export async function getMediaLibrary() {
  return parseResponse(await fetch(`${API_URL}/api/v1/media`, { signal: AbortSignal.timeout(15000) }));
}

export async function scanMediaLibrary() {
  return parseResponse(await fetch(`${API_URL}/api/v1/media/scan`, { method: "POST", signal: AbortSignal.timeout(30000) }));
}

export async function downloadMediaDataset(dryRun = false, maxAssets = 20) {
  const query = new URLSearchParams({ dry_run: String(dryRun), max_assets: String(maxAssets) });
  return parseResponse(await fetch(`${API_URL}/api/v1/media/download?${query}`, { method: "POST", signal: AbortSignal.timeout(180000) }));
}

export async function getJob(jobId) {
  const response = await fetch(`${API_URL}/api/v1/jobs/${encodeURIComponent(jobId)}`, {
    signal: AbortSignal.timeout(15000),
  });
  return parseResponse(response);
}

export async function getTimeline(jobId) {
  const response = await fetch(`${API_URL}/api/v1/jobs/${encodeURIComponent(jobId)}/timeline`, {
    signal: AbortSignal.timeout(15000),
  });
  return parseResponse(response);
}

export async function submitJob(body, multipart = false) {
  const response = await fetch(`${API_URL}/api/v1/broll/jobs`, {
    method: "POST",
    headers: multipart ? undefined : { "Content-Type": "application/json" },
    body: multipart ? body : JSON.stringify(body),
    signal: AbortSignal.timeout(30000),
  });
  return parseResponse(response);
}

export function fileUrl(path) {
  return new URL(path, `${API_URL}/`).toString();
}

export function getErrorMessage(error, fallback) {
  if (error?.name === "TimeoutError" || error?.name === "AbortError") {
    return "The request timed out. Check the backend connection and try again.";
  }
  if (error instanceof TypeError) {
    return "Could not reach the backend. Check that the API is running and try again.";
  }
  return error?.message || fallback;
}
