// Regression tests for the audit's frontend probes F-1 to F-10 and related findings.
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { ResponseBlocks } from "./App";
import { api, ApiError, setCsrfToken } from "./api";
import type { GenerationSnapshot, Message, ThreadDetail, ThreadSummary } from "./types";

const A = "5d0f7a3c-8e21-4b6d-9f14-2c7e0a9b1d01";
const B = "5d0f7a3c-8e21-4b6d-9f14-2c7e0a9b1d02";
const T = "5d0f7a3c-8e21-4b6d-9f14-2c7e0a9b1d03";
const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

class FakeEventSource {
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = 1;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, (event: MessageEvent<string>) => void>();
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.set(type, listener);
  }
  emit(type: string, data: unknown) {
    this.listeners.get(type)?.({ data: JSON.stringify(data) } as MessageEvent<string>);
  }
  /** What the browser does after a 401 or 404: close for good, then report an error. */
  fail() {
    this.readyState = FakeEventSource.CLOSED;
    this.onerror?.();
  }
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
  setCsrfToken("");
  document.cookie = "oveo_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => { resolve = settle; });
  return { promise, resolve };
}

const summary = (id: string, title: string): ThreadSummary => ({
  id, owner_id: "user-1", mode: "translate", title, updated_at: "2026-09-16T00:00:00Z", active_generation_id: null,
});

const message = (id: string, ordinal: number, text: string, role: Message["role"] = "user"): Message => ({
  id, ordinal, role, actor_username: null, blocks: [{ type: "conversation", text }], attachment: null, created_at: "2026-09-16T00:00:00Z",
});

const detailFor = (id: string, messages: Message[], extra: Partial<ThreadDetail> = {}): ThreadDetail => ({
  ...summary(id, `Thread ${id}`), owner_username: "charles", docx_exportable: false, generation: null, messages, ...extra,
});

const snapshot = (id: string, threadId: string, status: GenerationSnapshot["status"], seq: number, extra: Partial<GenerationSnapshot> = {}): GenerationSnapshot => ({
  id, thread_id: threadId, status, blocks: [], seq, ...extra,
});

const account = (username: "charles" | "yousra") => ({ id: `${username}-id`, username, display_name: username, csrf_token: `${username}-csrf` });

function signedIn(threads: ThreadSummary[]) {
  vi.spyOn(api, "me").mockResolvedValue(account("charles"));
  vi.spyOn(api, "usage").mockResolvedValue({ formatted: "$0.00" });
  vi.spyOn(api, "threads").mockResolvedValue(threads);
}

const fileInput = () => document.querySelector('input[type="file"]') as HTMLInputElement;

describe("M-2 / F-1: every sign-in starts from nothing", () => {
  it("drops the previous account's list, conversation, title and stream at sign-out", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${A}`);
    let current: "charles" | "yousra" | null = "charles";
    vi.spyOn(api, "me").mockImplementation(async () => {
      if (!current) throw new ApiError(401, "authentication_required", "Sign in to continue.");
      return account(current);
    });
    vi.spyOn(api, "usage").mockResolvedValue({ formatted: "$0.00" });
    vi.spyOn(api, "threads").mockImplementation(() =>
      current === "charles" ? Promise.resolve([summary(A, "Charles private layoff memo")]) : new Promise(() => undefined),
    );
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(A, [message("m1", 1, "Private draft")], {
      title: "Charles private layoff memo",
      generation: snapshot("g1", A, "running", 2),
    }));
    vi.spyOn(api, "logout").mockImplementation(async () => { current = null; });
    vi.spyOn(api, "login").mockImplementation(async () => { current = "yousra"; return account("yousra"); });

    render(<App />);
    expect(await screen.findByText("Private draft")).toBeInTheDocument();
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    expect(document.title).toBe("Charles private layoff memo · Oveo");

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await screen.findByRole("form", { name: "Sign in to Oveo" });
    expect(FakeEventSource.instances[0].readyState).toBe(FakeEventSource.CLOSED);
    expect(document.title).toBe("Oveo");
    expect(window.location.pathname).toBe("/");

    await user.type(screen.getByLabelText("Username"), "yousra");
    await user.type(screen.getByLabelText("Password"), "synthetic");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    // Yousra's own list never arrives in this test; nothing of Charles's may fill the gap.
    expect(await screen.findByRole("heading", { name: "What would you like to do?" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Conversations" })).not.toHaveTextContent("Charles private layoff memo");
    expect(screen.queryByText("Private draft")).not.toBeInTheDocument();
    expect(window.location.pathname).toBe("/new");
  });

  it("closes the workspace and its streams when the session ends elsewhere (401)", async () => {
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "Req")], { generation: snapshot("g1", T, "running", 2) }));
    render(<App />);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    act(() => { window.dispatchEvent(new Event("oveo:unauthorized")); });
    expect(await screen.findByRole("form", { name: "Sign in to Oveo" })).toBeInTheDocument();
    expect(FakeEventSource.instances[0].readyState).toBe(FakeEventSource.CLOSED);
    expect(screen.queryByText("Req")).not.toBeInTheDocument();
  });
});

describe("L-1 / F-2, F-9: responses cannot land on the wrong conversation", () => {
  it("shows and sends to the conversation in the address when loads finish out of order", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "/new");
    signedIn([summary(A, "Thread A"), summary(B, "Thread B")]);
    const loads: Record<string, ReturnType<typeof deferred<ThreadDetail>>> = { [A]: deferred(), [B]: deferred() };
    vi.spyOn(api, "thread").mockImplementation((id: string) => loads[id].promise);
    const submit = vi.spyOn(api, "submit").mockResolvedValue({ thread_id: B, generation_id: "g" });

    render(<App />);
    await screen.findByText("Thread A");
    act(() => {
      window.history.pushState(null, "", `/conversations/${A}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
      window.history.pushState(null, "", `/conversations/${B}`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await act(async () => { loads[B].resolve(detailFor(B, [message("mb", 1, "Message that belongs to B")])); });
    await act(async () => { loads[A].resolve(detailFor(A, [message("ma", 1, "Message that belongs to A")])); });

    expect(window.location.pathname).toBe(`/conversations/${B}`);
    expect(await screen.findByText("Message that belongs to B")).toBeInTheDocument();
    expect(screen.queryByText("Message that belongs to A")).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Intended for B");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(submit).toHaveBeenCalled());
    expect(submit.mock.calls[0][0].threadId).toBe(B);
  });

  it("does not show a conversation's composer while another one is loading", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${A}`);
    signedIn([summary(A, "Thread A"), summary(B, "Thread B")]);
    const loadB = deferred<ThreadDetail>();
    vi.spyOn(api, "thread").mockImplementation((id: string) =>
      id === A ? Promise.resolve(detailFor(A, [message("ma", 1, "Message in A")])) : loadB.promise,
    );
    render(<App />);
    await screen.findByText("Message in A");
    await user.click(screen.getByRole("button", { name: /^Thread B/ }));
    expect(screen.getByText("Loading conversation…")).toBeInTheDocument();
    expect(screen.queryByText("Message in A")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
  });

  it("leaves the user where they are when a send finishes after they moved on", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${A}`);
    signedIn([summary(A, "Thread A"), summary(B, "Thread B")]);
    vi.spyOn(api, "thread").mockImplementation(async (id: string) =>
      detailFor(id, [message(`m-${id}`, 1, id === A ? "Message in A" : "Message in B")]),
    );
    const accepted = deferred<{ thread_id: string; generation_id: string }>();
    vi.spyOn(api, "submit").mockReturnValue(accepted.promise);

    render(<App />);
    await screen.findByText("Message in A");
    await user.type(screen.getByRole("textbox", { name: "Message" }), "For A");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await user.click(screen.getByRole("button", { name: /^Thread B/ }));
    await screen.findByText("Message in B");
    await act(async () => { accepted.resolve({ thread_id: A, generation_id: "g" }); });

    expect(window.location.pathname).toBe(`/conversations/${B}`);
    expect(screen.getByText("Message in B")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Thread A/ }));
    await screen.findByText("Message in A");
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
  });

  it("reports an accepted message as sent when only the refresh afterwards fails", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread")
      .mockResolvedValueOnce(detailFor(T, [message("m1", 1, "Earlier")]))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const submit = vi.spyOn(api, "submit").mockResolvedValue({ thread_id: T, generation_id: "g2" });

    render(<App />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "Please translate this");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    expect(await screen.findByText(/Your message was sent, but the conversation could not be refreshed/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(submit).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
    // The accepted turn is still followed, so the page catches up by itself.
    await waitFor(() => expect(FakeEventSource.instances.at(-1)?.url).toBe("/api/generations/g2/events"));
  });

  it("opens a new conversation once its first message is accepted", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "/new");
    signedIn([]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "First request")]));
    const submit = vi.spyOn(api, "submit").mockResolvedValue({ thread_id: T, generation_id: "g1" });

    render(<App />);
    await user.click(await screen.findByRole("button", { name: /Translate/ }));
    await user.type(screen.getByRole("textbox", { name: "Message" }), "First request");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    expect(await screen.findByText("First request", { selector: "p" })).toBeInTheDocument();
    expect(submit.mock.calls[0][0]).toMatchObject({ mode: "translate", threadId: undefined });
    expect(window.location.pathname).toBe(`/conversations/${T}`);
  });
});

describe("M-5 (client): an unconfirmed send is repeated, never duplicated", () => {
  it("reuses the request identifier after a failure with an unknown outcome", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "Earlier")]));
    const submit = vi.spyOn(api, "submit")
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce({ thread_id: T, generation_id: "g2" });

    render(<App />);
    await user.type(await screen.findByRole("textbox", { name: "Message" }), "Hello");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not confirm that your message was sent");
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Hello");

    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit.mock.calls[1][0].clientRequestId).toBe(submit.mock.calls[0][0].clientRequestId);
  });

  it("starts a new request once the unsent message is changed, and after success", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "Earlier")]));
    const submit = vi.spyOn(api, "submit")
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValue({ thread_id: T, generation_id: "g2" });

    render(<App />);
    const textbox = await screen.findByRole("textbox", { name: "Message" });
    await user.type(textbox, "Hello");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByRole("alert");
    await user.type(textbox, " again");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    const [first, second] = submit.mock.calls.map(([args]) => args.clientRequestId);
    expect(second).not.toBe(first);
  });

  it("repeats an unconfirmed retry with the same identifier", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "Req")], {
      generation: snapshot("g1", T, "failed", 3, { error_message: "The model provider could not be reached.", retryable: true }),
    }));
    const retry = vi.spyOn(api, "retry")
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce({ generation_id: "g2" });

    render(<App />);
    await user.click(await screen.findByRole("button", { name: /Retry/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be retried");
    await user.click(screen.getByRole("button", { name: /Retry/ }));
    await waitFor(() => expect(retry).toHaveBeenCalledTimes(2));
    expect(retry.mock.calls[1][1]).toBe(retry.mock.calls[0][1]);
    await waitFor(() => expect(FakeEventSource.instances.at(-1)?.url).toBe("/api/generations/g2/events"));
  });
});

describe("L-7 / F-10: drafts stay with their conversation", () => {
  it("keeps each conversation's unsent text and attachment to itself", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${A}`);
    signedIn([summary(A, "Thread A"), summary(B, "Thread B")]);
    vi.spyOn(api, "thread").mockImplementation(async (id: string) =>
      detailFor(id, [message(`m-${id}`, 1, id === A ? "Message in A" : "Message in B")]),
    );
    const submit = vi.spyOn(api, "submit").mockResolvedValue({ thread_id: A, generation_id: "g" });

    render(<App />);
    await screen.findByText("Message in A");
    await user.type(screen.getByRole("textbox", { name: "Message" }), "Draft written for A");
    await user.upload(fileInput(), new File(["pkg"], "contract-A.docx", { type: DOCX }));

    await user.click(screen.getByRole("button", { name: /^Thread B/ }));
    await screen.findByText("Message in B");
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("");
    expect(screen.queryByText("contract-A.docx")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /^Thread A/ }));
    await screen.findByText("Message in A");
    expect(screen.getByRole("textbox", { name: "Message" })).toHaveValue("Draft written for A");
    expect(screen.getByText("contract-A.docx")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(submit.mock.calls[0][0]).toMatchObject({ threadId: A, text: "Draft written for A" });
  });
});

describe("L-10 / F-3, F-5, F-6, F-7: failures are reported and recoverable", () => {
  it("stops following, and cancels, a prompt handoff when its dialog is closed", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "Some request")]));
    vi.spyOn(api, "handoff").mockResolvedValue({ generation_id: "h" });
    const generation = vi.spyOn(api, "generation").mockResolvedValue(
      snapshot("h", T, "running", 1, { blocks: [{ type: "deliverable", text: "Partial" }] }),
    );
    const stop = vi.spyOn(api, "stop").mockResolvedValue(undefined);

    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Create prompt handoff" }));
    const dialog = await screen.findByRole("dialog", { name: "Prompt handoff" });
    await within(dialog).findByText("Partial");
    expect(within(dialog).getByText("Closing this dialog cancels the handoff.")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Close dialog" }));
    expect(stop).toHaveBeenCalledWith("h");
    const polls = generation.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 1000));
    expect(screen.queryByRole("dialog", { name: "Prompt handoff" })).not.toBeInTheDocument();
    expect(generation.mock.calls.length).toBe(polls);
  });

  it("follows a handoff that is already running instead of starting another", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "Some request")], {
      handoff: snapshot("h0", T, "running", 4),
    }));
    const create = vi.spyOn(api, "handoff");
    vi.spyOn(api, "generation").mockResolvedValue(
      snapshot("h0", T, "completed", 9, { blocks: [{ type: "deliverable", text: "Finished handoff" }] }),
    );

    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Create prompt handoff" }));
    const dialog = await screen.findByRole("dialog", { name: "Prompt handoff" });
    expect(await within(dialog).findByText("Finished handoff")).toBeInTheDocument();
    expect(create).not.toHaveBeenCalled();
    expect(api.generation).toHaveBeenCalledWith("h0");
  });

  it("sends the CSRF token from the current cookie, not the first one seen", async () => {
    setCsrfToken("token-from-first-login");
    document.cookie = "oveo_csrf=token-from-second-login; path=/";
    const headers: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      headers.push(new Headers(init.headers).get("X-CSRF-Token") ?? "");
      return new Response(null, { status: 204 });
    }));
    await api.stop("g");
    expect(headers).toEqual(["token-from-second-login"]);
  });

  it("reports a failed sign-out and lets the user try again", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "/new");
    signedIn([]);
    const logout = vi.spyOn(api, "logout")
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(undefined);

    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-out failed");
    const signOut = screen.getByRole("button", { name: "Sign out" });
    expect(signOut).toBeEnabled();
    await user.click(signOut);
    expect(await screen.findByRole("form", { name: "Sign in to Oveo" })).toBeInTheDocument();
    expect(logout).toHaveBeenCalledTimes(2);
  });

  it("shows why a retry or a stop was refused", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "Req")], {
      generation: snapshot("g1", T, "failed", 3, { error_message: "The model provider could not be reached.", retryable: true }),
    }));
    vi.spyOn(api, "retry").mockRejectedValue(new ApiError(409, "active_generation", "This conversation already has an active generation."));
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /Retry/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already has an active generation");
    cleanup();

    FakeEventSource.instances = [];
    vi.mocked(api.thread).mockResolvedValue(detailFor(T, [message("m1", 1, "Req")], { generation: snapshot("g1", T, "running", 2) }));
    vi.spyOn(api, "stop").mockRejectedValue(new TypeError("Failed to fetch"));
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Stop generation" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("could not be stopped");
    expect(screen.getByRole("button", { name: "Stop generation" })).toBeEnabled();
  });

  it("keeps the delete dialog open with the reason when deletion fails", async () => {
    const user = userEvent.setup();
    window.history.replaceState(null, "", "/new");
    signedIn([summary(A, "Thread A")]);
    const remove = vi.spyOn(api, "deleteThread")
      .mockRejectedValueOnce(new ApiError(500, "internal_error", "The request could not be completed."))
      .mockRejectedValueOnce(new ApiError(404, "thread_not_found", "Conversation not found."));

    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Delete Thread A" }));
    const dialog = screen.getByRole("dialog", { name: "Delete conversation?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete permanently" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The request could not be completed.");
    // Already gone (for example deleted in another tab) is what the user asked for.
    await user.click(within(dialog).getByRole("button", { name: "Delete permanently" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Delete conversation?" })).not.toBeInTheDocument());
    expect(remove).toHaveBeenCalledTimes(2);
  });

  it("says so when the clipboard refuses a copy", async () => {
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new DOMException("Denied", "NotAllowedError"));
    render(<ResponseBlocks blocks={[{ type: "deliverable", text: "Result" }]} />);
    await user.click(screen.getByRole("button", { name: "Copy deliverable" }));
    expect(await screen.findByRole("button", { name: "Copy failed" })).toBeInTheDocument();
  });

  it("keeps following a response after its event stream is closed for good", async () => {
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    const thread = vi.spyOn(api, "thread")
      .mockResolvedValueOnce(detailFor(T, [message("m1", 1, "Req")], { generation: snapshot("g1", T, "running", 2) }))
      .mockResolvedValue(detailFor(T, [message("m2", 2, "Answer", "assistant")]));
    vi.spyOn(api, "generation").mockResolvedValue(snapshot("g1", T, "completed", 5));

    render(<App />);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    expect(screen.getByRole("textbox", { name: "Message" })).toBeDisabled();
    act(() => { FakeEventSource.instances[0].fail(); });

    await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Send message" })).toBeInTheDocument();
    // L-2: the refresh after completion asks only for messages newer than the ones shown.
    expect(await screen.findByText("Answer")).toBeInTheDocument();
    expect(screen.getByText("Req")).toBeInTheDocument();
    expect(thread).toHaveBeenLastCalledWith(T, 1);
  });

  it("unlocks the page when the response's conversation was deleted elsewhere", async () => {
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread")
      .mockResolvedValueOnce(detailFor(T, [message("m1", 1, "Req")], { generation: snapshot("g1", T, "running", 2) }))
      .mockRejectedValue(new ApiError(404, "thread_not_found", "Conversation not found."));
    vi.spyOn(api, "generation").mockRejectedValue(new ApiError(404, "generation_not_found", "Generation not found."));

    render(<App />);
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    act(() => { FakeEventSource.instances[0].fail(); });
    expect(await screen.findByRole("alert")).toHaveTextContent("This conversation no longer exists.");
    expect(window.location.pathname).toBe("/new");
    expect(screen.getByRole("heading", { name: "What would you like to do?" })).toBeInTheDocument();
  });
});

describe("L-14 / F-4: identifiers never address another route", () => {
  it("treats a non-conversation address as a new conversation without requesting it", async () => {
    window.history.replaceState(null, "", "/conversations/..%2Fauth%2Fme");
    signedIn([]);
    const thread = vi.spyOn(api, "thread");
    render(<App />);
    expect(await screen.findByRole("heading", { name: "What would you like to do?" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/new");
    expect(thread).not.toHaveBeenCalled();
  });

  it("encodes every identifier as a single path segment", async () => {
    const requested: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      requested.push(url);
      return new Response(null, { status: 204 });
    }));
    await api.thread("../auth/me");
    await api.thread("x/y", 3);
    await api.generation("a/b");
    await api.stop("../../auth/logout");
    await api.submit({ threadId: "t/../x", text: "", clientRequestId: "r" });
    expect(requested).toEqual([
      "/api/threads/..%2Fauth%2Fme",
      "/api/threads/x%2Fy?after_ordinal=3",
      "/api/generations/a%2Fb",
      "/api/generations/..%2F..%2Fauth%2Flogout/stop",
      "/api/threads/t%2F..%2Fx/messages",
    ]);
    expect(new URL(requested[0], "https://oveo.example/").pathname).toBe("/api/threads/..%2Fauth%2Fme");
  });
});

describe("L-16 / F-8: accessibility", () => {
  it("announces status changes without reading streamed text aloud", async () => {
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([summary(T, "Thread T")]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "Req")], {
      generation: snapshot("g1", T, "running", 2, { blocks: [{ type: "deliverable", text: "Streaming draft" }] }),
    }));

    render(<App />);
    const draft = await screen.findByText("Streaming draft");
    expect(draft.closest("[aria-live]")).toBeNull();
    const status = screen.getAllByRole("status").find((element) => element.classList.contains("visually-hidden"));
    await waitFor(() => expect(status).toHaveTextContent("Oveo is writing a response."));

    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    act(() => {
      FakeEventSource.instances[0].emit("snapshot", snapshot("g1", T, "completed", 7, { blocks: [{ type: "deliverable", text: "Streaming draft, finished" }] }));
    });
    await waitFor(() => expect(status).toHaveTextContent("Response complete."));
    expect(status).not.toHaveTextContent("Streaming draft");
  });

  it("keeps the closed mobile sidebar out of reach and reports whether it is open", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
      matches: query === "(max-width: 760px)",
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    window.history.replaceState(null, "", "/new");
    signedIn([summary(A, "Thread A")]);

    render(<App />);
    await screen.findByText("Thread A");
    const sidebar = document.querySelector("aside") as HTMLElement;
    const menu = screen.getByRole("button", { name: "Open sidebar" });
    expect(sidebar).toHaveAttribute("inert");
    expect(menu).toHaveAttribute("aria-expanded", "false");
    expect(menu).toHaveAttribute("aria-controls", sidebar.id);

    await user.click(menu);
    expect(sidebar).not.toHaveAttribute("inert");
    expect(menu).toHaveAttribute("aria-expanded", "true");
  });

  it("labels the activity indicator and the streaming caret as images", async () => {
    window.history.replaceState(null, "", `/conversations/${T}`);
    signedIn([{ ...summary(T, "Thread T"), active_generation_id: "g1" }]);
    vi.spyOn(api, "thread").mockResolvedValue(detailFor(T, [message("m1", 1, "Req")], { generation: snapshot("g1", T, "running", 2) }));
    render(<App />);
    await waitFor(() => expect(screen.getAllByRole("img", { name: "Generating" })).toHaveLength(2));
  });
});

describe("L-2 (client): the conversation list is polled gently", () => {
  it("polls every 15 seconds and only while the tab is visible", async () => {
    const intervals = vi.spyOn(window, "setInterval");
    window.history.replaceState(null, "", "/new");
    signedIn([]);
    render(<App />);
    await screen.findByRole("heading", { name: "What would you like to do?" });

    const listPoll = intervals.mock.calls.find(([, ms]) => ms === 15_000);
    expect(listPoll).toBeDefined();
    const tick = listPoll![0] as () => void;
    const threads = vi.mocked(api.threads);
    const before = threads.mock.calls.length;
    let visibility: DocumentVisibilityState = "hidden";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
    try {
      act(() => tick());
      expect(threads.mock.calls.length).toBe(before);
      visibility = "visible";
      act(() => tick());
      expect(threads.mock.calls.length).toBe(before + 1);
    } finally {
      delete (document as { visibilityState?: DocumentVisibilityState }).visibilityState;
    }
  });
});
