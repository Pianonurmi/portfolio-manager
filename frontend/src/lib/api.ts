// Typed fetch wrapper. Mutations carry X-PM-Request (CSRF guard, see backend auth.py).

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type Opts = { method?: string; body?: unknown; signal?: AbortSignal; form?: FormData };

export async function api<T>(path: string, opts: Opts = {}): Promise<T> {
  const method = opts.method ?? (opts.body !== undefined || opts.form ? "POST" : "GET");
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-PM-Request"] = "1";
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, { method, headers, body, signal: opts.signal, credentials: "same-origin" });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) {
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : null;
    const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map(describe).join("; ") : res.statusText;
    if (res.status === 401) window.dispatchEvent(new CustomEvent("pm:unauthorized"));
    throw new ApiError(res.status, msg || `Request failed (${res.status})`);
  }
  return data as T;
}

function safeJson(t: string): unknown {
  try {
    return JSON.parse(t);
  } catch {
    return t;
  }
}

function describe(d: unknown): string {
  if (d && typeof d === "object" && "msg" in d) {
    const loc = "loc" in d && Array.isArray((d as { loc: unknown[] }).loc) ? (d as { loc: unknown[] }).loc.slice(1).join(".") : "";
    return `${loc ? loc + ": " : ""}${(d as { msg: string }).msg}`;
  }
  return String(d);
}
