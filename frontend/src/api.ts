import type { Account, GenerationSnapshot, SessionUser, ThreadDetail, ThreadSummary } from "./types";

let csrfToken = "";

function cookie(name: string): string {
  const prefix = `${encodeURIComponent(name)}=`;
  const entry = document.cookie.split("; ").find((part) => part.startsWith(prefix));
  return entry ? decodeURIComponent(entry.slice(prefix.length)) : "";
}

export function setCsrfToken(token: string): void {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (init.method && !["GET", "HEAD"].includes(init.method.toUpperCase())) {
    headers.set("X-CSRF-Token", csrfToken || cookie("oveo_csrf"));
  }
  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (response.status === 401) window.dispatchEvent(new Event("oveo:unauthorized"));
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, body.code ?? "request_failed", body.message ?? "Request failed.");
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  me: () => request<SessionUser>("/api/auth/me"),
  login: (username: string, password: string) =>
    request<SessionUser>("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  accounts: () => request<Account[]>("/api/accounts"),
  threads: () => request<ThreadSummary[]>("/api/threads"),
  thread: (id: string) => request<ThreadDetail>(`/api/threads/${id}`),
  rename: (id: string, title: string) =>
    request<ThreadSummary>(`/api/threads/${id}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  deleteThread: (id: string) => request<void>(`/api/threads/${id}`, { method: "DELETE" }),
  submit: async (args: {
    threadId?: string;
    mode?: string;
    text: string;
    attachment?: File;
    clientRequestId: string;
  }) => {
    const form = new FormData();
    form.set("text", args.text);
    form.set("client_request_id", args.clientRequestId);
    if (args.mode) form.set("mode", args.mode);
    if (args.attachment) form.set("attachment", args.attachment);
    const path = args.threadId ? `/api/threads/${args.threadId}/messages` : "/api/threads";
    return request<{ thread_id: string; generation_id: string }>(path, { method: "POST", body: form });
  },
  stop: (id: string) => request<void>(`/api/generations/${id}/stop`, { method: "POST" }),
  retry: (id: string, clientRequestId: string) =>
    request<{ generation_id: string }>(`/api/generations/${id}/retry`, {
      method: "POST",
      body: JSON.stringify({ client_request_id: clientRequestId }),
    }),
  handoff: (threadId: string, clientRequestId: string) =>
    request<{ generation_id: string }>(`/api/threads/${threadId}/prompt-handoff`, {
      method: "POST",
      body: JSON.stringify({ client_request_id: clientRequestId }),
    }),
  generation: (id: string) => request<GenerationSnapshot>(`/api/generations/${id}`),
  usage: () => request<{ formatted: string }>("/api/usage/lifetime"),
};
