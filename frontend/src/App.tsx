import {
  Check,
  Copy,
  Download,
  FileText,
  FilePenLine,
  Languages,
  LogOut,
  Megaphone,
  Menu,
  Paperclip,
  Plus,
  RotateCcw,
  Send,
  Sparkles,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { type FormEvent, type KeyboardEvent, type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, ApiError, setCsrfToken } from "./api";
import BrandLogo from "./BrandLogo";
import { followGeneration, isTerminal } from "./stream";
import type {
  AttachmentRole,
  ContentBlock,
  GenerationSnapshot,
  Mode,
  SessionUser,
  ThreadDetail,
  ThreadSummary,
} from "./types";

const uuid = () => crypto.randomUUID();
// Conversation IDs are UUIDs; anything else in the address is not a conversation and
// never reaches an API path.
const CONVERSATION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// The list only shows titles and activity, so a hidden tab does not poll at all.
const THREAD_LIST_POLL_MS = 15_000;
const HANDOFF_POLL_MS = 700;
// Matches the breakpoint in styles.css where the sidebar becomes an off-canvas panel.
const MOBILE_LAYOUT = "(max-width: 760px)";
const UNCONFIRMED_SEND = "Oveo could not confirm that your message was sent. Send it again; it will not be duplicated.";

const MODE_COPY: Record<Mode, { label: string; heading: string; guidance: string }> = {
  translate: {
    label: "Translate",
    heading: "What would you like to translate?",
    guidance: "Share the French or English source text. If an English source has no French target yet, Oveo will ask only which French variety you want.",
  },
  revision: {
    label: "Revision",
    heading: "What would you like to revise?",
    guidance: "Share the existing text and, when it matters, the depth you want: proofread, copyedit, revise, or rewrite. To review an existing translation, include both the original and translated text.",
  },
  internal_comms: {
    label: "Communications",
    heading: "What internal communication do you need?",
    guidance: "Share the brief, known facts, audience, desired locale, and any practical constraints. Oveo will not invent missing details.",
  },
};

export function conversationPath(id: string) {
  return `/conversations/${encodeURIComponent(id)}`;
}

export function conversationIdFromPath(pathname: string) {
  const match = /^\/conversations\/([^/]+)\/?$/.exec(pathname);
  if (!match) return null;
  let id: string;
  try {
    id = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  return CONVERSATION_ID.test(id) ? id.toLowerCase() : null;
}

export function canonicalPath(pathname: string, authenticated: boolean) {
  if (!authenticated) return "/";
  if (pathname === "/new") return pathname;
  const conversationId = conversationIdFromPath(pathname);
  return conversationId ? conversationPath(conversationId) : "/new";
}

function updateAddress(path: string, replace = false) {
  if (window.location.pathname === path) return;
  window.history[replace ? "replaceState" : "pushState"](null, "", path);
}

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof ApiError ? reason.message : fallback;
}

function isUnauthorized(reason: unknown) {
  return reason instanceof ApiError && reason.status === 401;
}

function delay(ms: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, ms));
}

/** What the workspace shows: a new conversation (with or without a chosen mode) or one conversation. */
type View = { kind: "new"; mode: Mode | null } | { kind: "thread"; id: string };

function viewFromPath(pathname: string): View {
  const id = conversationIdFromPath(pathname);
  return id ? { kind: "thread", id } : { kind: "new", mode: null };
}

function viewKey(view: View) {
  return view.kind === "thread" ? `thread:${view.id}` : `new:${view.mode ?? ""}`;
}

/** The newest message ordinal shown, or undefined when a message has none (older server). */
function knownOrdinal(detail: ThreadDetail): number | undefined {
  let highest = 0;
  for (const message of detail.messages) {
    if (typeof message.ordinal !== "number") return undefined;
    highest = Math.max(highest, message.ordinal);
  }
  return highest;
}

/** Add the messages of an incremental refresh to the conversation already shown. */
export function mergeDetail(previous: ThreadDetail | undefined, loaded: ThreadDetail, incremental: boolean): ThreadDetail {
  if (!incremental || !previous || previous.id !== loaded.id) return loaded;
  const shown = new Set(previous.messages.map((message) => message.id));
  return { ...loaded, messages: [...previous.messages, ...loaded.messages.filter((message) => !shown.has(message.id))] };
}

/** A refresh can return an older copy of the generation the stream already advanced. */
function newerGeneration(previous: GenerationSnapshot | null, loaded: GenerationSnapshot | null) {
  return previous && loaded && previous.id === loaded.id && previous.seq > loaded.seq ? previous : loaded;
}

function statusAnnouncement(status: GenerationSnapshot["status"], error?: string | null) {
  switch (status) {
    case "queued":
    case "running":
      return "Oveo is writing a response.";
    case "stopping":
      return "Stopping the response.";
    case "completed":
      return "Response complete.";
    case "stopped":
      return "Response stopped.";
    case "failed":
      return `Response failed. ${error ?? "Oveo could not complete this response."}`;
  }
}

interface WebMcpContext {
  registerTool(tool: {
    name: string;
    title: string;
    description: string;
    inputSchema: object;
    annotations: { readOnlyHint: boolean; untrustedContentHint: boolean };
    execute(input: unknown): Promise<unknown>;
  }, options: { signal: AbortSignal }): void | Promise<void>;
}

export function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const closeHandler = useRef(onClose);
  const titleId = useId();
  useEffect(() => { closeHandler.current = onClose; }, [onClose]);
  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const keyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeHandler.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? []);
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      } else if (!dialogRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", keyDown);
    return () => {
      document.removeEventListener("keydown", keyDown);
      previousFocus?.focus();
    };
  }, []);
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section ref={dialogRef} className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <header>
          <h2 id={titleId}>{title}</h2>
          <button ref={closeRef} className="icon-button" onClick={onClose} aria-label="Close dialog"><X /></button>
        </header>
        {children}
      </section>
    </div>
  );
}

export function Login({ onLogin }: { onLogin: (user: SessionUser) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const user = await api.login(username.trim(), password);
      if (user.csrf_token) setCsrfToken(user.csrf_token);
      onLogin(user);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "Sign-in failed. Please try again.");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-page">
      <form className="login-card" aria-label="Sign in to Oveo" onSubmit={submit}>
        <BrandLogo />
        <label>Username<input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        {error && <div className="form-error" role="alert">{error}</div>}
        <button className="primary-button" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </main>
  );
}

function ModeBadge({ mode }: { mode: Mode }) {
  return <span className={`mode-badge ${mode}`}>{MODE_COPY[mode].label}</span>;
}

function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      allowedElements={["p", "strong", "em", "ul", "ol", "li", "a", "code", "blockquote", "br"]}
      unwrapDisallowed
      components={{ a: ({ href, children }) => <a href={href} rel="noreferrer" target="_blank">{children}</a> }}
    >
      {text}
    </ReactMarkdown>
  );
}

export function ResponseBlocks({ blocks, streaming = false }: { blocks: ContentBlock[]; streaming?: boolean }) {
  const [copied, setCopied] = useState<{ index: number; ok: boolean } | null>(null);
  const visibleBlocks = streaming ? blocks.filter((block) => block.text.trim().length > 0) : blocks;
  async function copy(text: string, index: number) {
    let ok = true;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard access can be refused (permissions, insecure context); say so.
      ok = false;
    }
    setCopied({ index, ok });
    window.setTimeout(() => setCopied(null), 1600);
  }
  const copyLabel = (index: number) => copied?.index !== index ? "Copy deliverable" : copied.ok ? "Copied" : "Copy failed";
  return (
    <div className={`response-blocks${streaming ? " streaming" : ""}`}>
      {visibleBlocks.map((block, index) => block.type === "deliverable" ? (
        <section className="deliverable" key={index} aria-label="Deliverable">
          <div className="copy-button-rail">
            <button
              className="copy-button"
              onClick={() => void copy(block.text, index)}
              aria-label={copyLabel(index)}
              title={copyLabel(index)}
            >
              {copied?.index === index && copied.ok ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
            </button>
          </div>
          <pre>{block.text}</pre>
        </section>
      ) : (
        <section className={block.type} key={index}>
          {block.type === "advice" && <h3>Notes</h3>}
          <Markdown text={block.text} />
        </section>
      ))}
      {streaming && <span className="stream-caret" role="img" aria-label="Generating" />}
    </div>
  );
}

export function MessageList({ detail, generation }: { detail: ThreadDetail; generation: GenerationSnapshot | null }) {
  const messagesRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const messages = messagesRef.current;
    if (messages) messages.scrollTop = messages.scrollHeight;
  }, [detail.messages.length, generation?.seq]);
  // Not a live region: streamed text would be read out chunk by chunk. The workspace
  // announces coarse status changes in its own status region instead.
  return (
    <div ref={messagesRef} className="messages">
      {detail.messages.map((message) => (
        <article className={`message ${message.role}`} key={message.id}>
          <div className="message-inner">
            {message.role === "assistant" ? <ResponseBlocks blocks={message.blocks} /> : (
              <>
                {message.actor_username && message.actor_username !== detail.owner_username && <div className="message-author">{message.actor_username}</div>}
                {message.blocks.map((block, index) => <p className="user-text" key={index}>{block.text}</p>)}
                {message.attachment && <div className="sent-attachment"><FileText /> <span>{message.attachment.filename}</span><small>{message.attachment.role === "reference" ? "Reference" : "Source"}</small></div>}
              </>
            )}
          </div>
        </article>
      ))}
      {generation && !isTerminal(generation.status) && (
        <article className="message assistant pending"><div className="message-inner"><ResponseBlocks blocks={generation.blocks} streaming /></div></article>
      )}
      {generation?.status === "failed" && generation.blocks.length > 0 && (
        <article className="message assistant preserved"><div className="message-inner"><ResponseBlocks blocks={generation.blocks} /></div></article>
      )}
      {generation && ["failed", "stopped"].includes(generation.status) && (
        <div className="generation-notice">
          <span>{generation.status === "stopped" ? "Generation stopped." : generation.error_message ?? "Oveo could not complete this response."}</span>
        </div>
      )}
    </div>
  );
}

export interface ComposerDraft {
  text: string;
  attachment?: File;
  attachmentRole: AttachmentRole;
}

export function Composer({
  activeGeneration,
  readDraft,
  onDraftChange,
  onSend,
  onStop,
  onRetry,
  onCreateHandoff,
}: {
  activeGeneration: GenerationSnapshot | null;
  /** The unsent draft this conversation had when its composer was last shown. */
  readDraft?: () => ComposerDraft | undefined;
  onDraftChange?: (draft: ComposerDraft) => void;
  onSend: (text: string, attachment?: File, attachmentRole?: AttachmentRole) => Promise<void>;
  onStop: () => Promise<void>;
  onRetry: () => Promise<void>;
  onCreateHandoff?: () => void;
}) {
  const [draft, setDraft] = useState<ComposerDraft>(() => readDraft?.() ?? { text: "", attachmentRole: "source" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"send" | "stop" | "retry" | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const { text, attachment, attachmentRole } = draft;
  const generating = Boolean(activeGeneration && !isTerminal(activeGeneration.status));
  const retryable = Boolean(activeGeneration && ["failed", "stopped"].includes(activeGeneration.status));

  function change(next: Partial<ComposerDraft>) {
    const merged = { ...draft, ...next };
    setDraft(merged);
    onDraftChange?.(merged);
  }
  function acceptFile(file?: File) {
    if (!file) return;
    if (attachment) return setError("Remove the current attachment before adding another.");
    if (!/\.docx$/i.test(file.name)) return setError("Only .docx files are supported.");
    change({ attachment: file, attachmentRole: "source" });
    setError("");
  }
  function removeAttachment() {
    change({ attachment: undefined, attachmentRole: "source" });
    if (fileRef.current) fileRef.current.value = "";
  }
  async function send() {
    if ((!text.trim() && !attachment) || busy || generating) return;
    setBusy("send");
    setError("");
    try {
      if (attachment) await onSend(text, attachment, attachmentRole);
      else await onSend(text);
      change({ text: "", attachment: undefined, attachmentRole: "source" });
      if (fileRef.current) fileRef.current.value = "";
    } catch (reason) {
      // Without an answer from the server the message may or may not have arrived. The
      // text stays here, and sending it again reuses the same request identifier.
      setError(errorMessage(reason, UNCONFIRMED_SEND));
    } finally {
      setBusy(null);
    }
  }
  async function run(action: "stop" | "retry", call: () => Promise<void>, fallback: string) {
    if (busy) return;
    setBusy(action);
    setError("");
    try {
      await call();
    } catch (reason) {
      setError(errorMessage(reason, fallback));
    } finally {
      setBusy(null);
    }
  }
  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  }
  return (
    <div className="composer-wrap">
      {retryable && (
        <button className="retry-button" onClick={() => void run("retry", onRetry, "The response could not be retried. Try again.")} disabled={busy === "retry"}>
          <RotateCcw /> {busy === "retry" ? "Retrying…" : "Retry"}
        </button>
      )}
      <div className="composer">
        {attachment && <div className="attachment-chip"><FileText /><span>{attachment.name}</span><select aria-label="Use attachment as" value={attachmentRole} onChange={(event) => change({ attachmentRole: event.target.value as AttachmentRole })}><option value="source">Source document</option><option value="reference">Style reference</option></select><button onClick={removeAttachment} aria-label="Remove attachment"><X /></button></div>}
        <textarea
          aria-label="Message"
          placeholder="Message"
          value={text}
          onChange={(event) => change({ text: event.target.value })}
          onKeyDown={keyDown}
          rows={3}
          disabled={generating}
        />
        <div className="composer-actions">
          {onCreateHandoff && <button className="icon-button handoff-button" onClick={onCreateHandoff} disabled={generating} aria-label="Create prompt handoff" title="Create prompt handoff"><Sparkles /></button>}
          <button className="icon-button attach" onClick={() => fileRef.current?.click()} disabled={generating} aria-label="Attach a DOCX file"><Paperclip /></button>
          <input ref={fileRef} className="file-input" type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={(e) => acceptFile(e.target.files?.[0])} />
          {generating ? (
            <button
              className="send-button stop"
              onClick={() => void run("stop", onStop, "The response could not be stopped. Try again.")}
              disabled={busy === "stop" || activeGeneration?.status === "stopping"}
              aria-label="Stop generation"
            >
              <Square />
            </button>
          ) : (
            <button className="send-button" onClick={() => void send()} disabled={busy !== null || (!text.trim() && !attachment)} aria-label="Send message"><Send /></button>
          )}
        </div>
      </div>
      {error && <div className="composer-error" role="alert">{error}</div>}
    </div>
  );
}

export function EmptyThread({ mode, onSelect }: { mode: Mode | null; onSelect: (mode: Mode) => void }) {
  return (
    <div className="empty-thread">
      <h1>{mode ? MODE_COPY[mode].heading : "What would you like to do?"}</h1>
      {mode === null ? (
        <div className="mode-choices" aria-label="Conversation type">
          <button type="button" onClick={() => onSelect("translate")}><span className="choice-icon"><Languages /></span><strong>{MODE_COPY.translate.label}</strong></button>
          <button type="button" onClick={() => onSelect("revision")}><span className="choice-icon"><FilePenLine /></span><strong>{MODE_COPY.revision.label}</strong></button>
          <button type="button" onClick={() => onSelect("internal_comms")}><span className="choice-icon"><Megaphone /></span><strong>{MODE_COPY.internal_comms.label}</strong></button>
        </div>
      ) : (
        <p>{MODE_COPY[mode].guidance}</p>
      )}
    </div>
  );
}

export function SidebarHeader({ onClose }: { onClose: () => void }) {
  return (
    <div className="sidebar-head">
      <button className="mobile-close icon-button" onClick={onClose} aria-label="Close sidebar"><X /></button>
    </div>
  );
}

interface HandoffView {
  threadId: string;
  generationId: string | null;
  blocks: ContentBlock[];
  finished: boolean;
}

/**
 * Everything one sign-in can see. The app renders a new instance for every sign-in,
 * so conversations, drafts, streams and timers are discarded with the session and can
 * never be shown to the next account.
 */
function Workspace({ user, onSignedOut }: { user: SessionUser; onSignedOut: () => void }) {
  const [view, setView] = useState<View>(() => viewFromPath(window.location.pathname));
  const [detail, setDetail] = useState<ThreadDetail>();
  const [generation, setGeneration] = useState<GenerationSnapshot | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [usage, setUsage] = useState("$0.00");
  const [deleteTarget, setDeleteTarget] = useState<ThreadSummary | null>(null);
  const [deletion, setDeletion] = useState({ busy: false, error: "" });
  const [handoff, setHandoff] = useState<HandoffView | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [mobileLayout, setMobileLayout] = useState(() => typeof window.matchMedia === "function" && window.matchMedia(MOBILE_LAYOUT).matches);
  const [globalError, setGlobalError] = useState("");
  const [notice, setNotice] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const [signingOut, setSigningOut] = useState(false);
  const sidebarId = useId();

  // Every navigation stores a new view object here, so a response that arrives for a
  // view the user has left can tell and is dropped.
  const viewRef = useRef(view);
  const detailRef = useRef(detail);
  // Changes whenever the shown generation is replaced by navigation, a send or a retry,
  // so an older refresh cannot put back the generation it read.
  const generationEpoch = useRef(0);
  const listRequest = useRef(0);
  const handoffRun = useRef(0);
  const lifetime = useRef<AbortSignal | null>(null);
  const drafts = useRef(new Map<string, ComposerDraft>());
  const requestIds = useRef(new Map<string, { id: string; parts: unknown[] }>());

  useEffect(() => { detailRef.current = detail; }, [detail]);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller.signal;
    return () => {
      controller.abort();
      document.title = "Oveo";
    };
  }, []);

  const refreshUsage = useCallback(() => {
    void api.usage().then((total) => setUsage(total.formatted)).catch(() => undefined);
  }, []);

  const refreshThreads = useCallback(async (reportFailure = false) => {
    const request = ++listRequest.current;
    try {
      const list = await api.threads();
      if (listRequest.current === request) setThreads(list);
    } catch (reason) {
      if (reportFailure && listRequest.current === request && !isUnauthorized(reason)) {
        setGlobalError(errorMessage(reason, "Could not load conversations."));
      }
    }
  }, []);

  const enter = useCallback((next: View, address: "push" | "replace" | "none") => {
    viewRef.current = next;
    generationEpoch.current += 1;
    setView(next);
    setDetail(undefined);
    setGeneration(null);
    setSidebarOpen(false);
    if (address !== "none") updateAddress(next.kind === "thread" ? conversationPath(next.id) : "/new", address === "replace");
  }, []);

  const openView = useCallback((next: View, address: "push" | "replace" | "none") => {
    enter(next, address);
    if (next.kind !== "thread") return;
    void api.thread(next.id).then(
      (loaded) => {
        if (viewRef.current !== next) return;
        setDetail(loaded);
        setGeneration(loaded.generation ?? null);
      },
      (reason: unknown) => {
        if (viewRef.current !== next || isUnauthorized(reason)) return;
        setGlobalError(errorMessage(reason, "Could not load that conversation."));
        enter({ kind: "new", mode: null }, "replace");
      },
    );
  }, [enter]);

  /** Fetch what is new in an open conversation. False only when the request failed. */
  const refreshDetail = useCallback(async (id: string): Promise<boolean> => {
    const target = viewRef.current;
    if (target.kind !== "thread" || target.id !== id) return true;
    const shown = detailRef.current?.id === id ? detailRef.current : undefined;
    const after = shown ? knownOrdinal(shown) : undefined;
    const epoch = generationEpoch.current;
    try {
      const loaded = await api.thread(id, after);
      if (viewRef.current !== target) return true;
      setDetail((previous) => mergeDetail(previous, loaded, after !== undefined));
      if (generationEpoch.current === epoch) {
        setGeneration((previous) => newerGeneration(previous, loaded.generation ?? null));
      }
      return true;
    } catch (reason) {
      if (viewRef.current !== target || isUnauthorized(reason)) return true;
      if (reason instanceof ApiError && reason.status === 404) {
        setGlobalError("This conversation no longer exists.");
        enter({ kind: "new", mode: null }, "replace");
        return true;
      }
      return false;
    }
  }, [enter]);

  const adoptGeneration = useCallback((snapshot: GenerationSnapshot) => {
    generationEpoch.current += 1;
    setGeneration(snapshot);
  }, []);

  useEffect(() => {
    void refreshThreads(true);
    refreshUsage();
    const poll = () => {
      if (document.visibilityState === "visible") void refreshThreads();
    };
    const timer = window.setInterval(poll, THREAD_LIST_POLL_MS);
    document.addEventListener("visibilitychange", poll);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", poll);
    };
  }, [refreshThreads, refreshUsage]);

  useEffect(() => {
    const applyAddress = () => {
      const path = canonicalPath(window.location.pathname, true);
      updateAddress(path, true);
      openView(viewFromPath(path), "none");
    };
    applyAddress();
    window.addEventListener("popstate", applyAddress);
    return () => window.removeEventListener("popstate", applyAddress);
  }, [openView]);

  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(MOBILE_LAYOUT);
    const update = () => setMobileLayout(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  const current = view.kind === "thread" && detail?.id === view.id ? detail : undefined;
  const active = current && generation?.thread_id === current.id ? generation : null;

  const title = current?.title;
  useEffect(() => {
    document.title = title ? `${title} · Oveo` : "Oveo";
  }, [title]);

  const activeId = active?.id;
  const activeStatus = active?.status;
  const activeError = active?.error_message;
  useEffect(() => {
    if (activeId && activeStatus) setAnnouncement(statusAnnouncement(activeStatus, activeError));
  }, [activeId, activeStatus, activeError]);

  const followedId = generation && !isTerminal(generation.status) ? generation.id : undefined;
  useEffect(() => {
    if (!followedId) return;
    return followGeneration(followedId, {
      onUpdate: (next) => {
        setGeneration((previous) => (previous?.id === next.id ? next : previous));
        if (!isTerminal(next.status)) return;
        if (next.thread_id) {
          void refreshDetail(next.thread_id).then((refreshed) => {
            if (!refreshed) setNotice("The response has finished, but the conversation could not be refreshed. Reload the page to see it.");
          });
        }
        void refreshThreads();
        refreshUsage();
      },
      onUnavailable: () => {
        // A 401 signs the whole app out; otherwise the generation is gone because its
        // conversation was deleted, which the refresh reports.
        setGeneration((previous) => (previous?.id === followedId ? null : previous));
        const target = viewRef.current;
        if (target.kind === "thread") void refreshDetail(target.id);
      },
    });
  }, [followedId, refreshDetail, refreshThreads, refreshUsage]);

  useEffect(() => {
    const context = (document as Document & { modelContext?: WebMcpContext }).modelContext;
    if (!context?.registerTool) return;
    const registration = new AbortController();
    void Promise.resolve(context.registerTool({
      name: "start_oveo_conversation",
      title: "Start an Oveo conversation",
      description: "Open a new unsaved Translate, Revision, or Communications conversation in the visible Oveo interface.",
      inputSchema: {
        type: "object",
        properties: {
          mode: { type: "string", enum: ["translate", "revision", "internal_comms"] },
        },
        required: ["mode"],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      async execute(input) {
        const value = input as { mode?: unknown };
        if (value.mode !== "translate" && value.mode !== "revision" && value.mode !== "internal_comms") throw new Error("Invalid mode.");
        openView({ kind: "new", mode: value.mode }, "push");
        return { state: "ready", mode: value.mode, ownerUsername: user.username };
      },
    }, { signal: registration.signal })).catch(() => undefined);
    return () => registration.abort();
  }, [openView, user.username]);

  /**
   * One identifier per unsent request: an attempt whose outcome is unknown (network
   * failure, lost response) is repeated with the same identifier, so the server returns
   * the turn it may already have accepted instead of creating a second one. Changing
   * what is sent starts a new request.
   */
  function requestIdFor(key: string, parts: unknown[]) {
    const known = requestIds.current.get(key);
    if (known && known.parts.length === parts.length && known.parts.every((part, index) => Object.is(part, parts[index]))) {
      return known.id;
    }
    const id = uuid();
    requestIds.current.set(key, { id, parts });
    return id;
  }

  async function send(origin: View, text: string, attachment?: File, attachmentRole?: AttachmentRole) {
    const key = viewKey(origin);
    const requestKey = `send:${key}`;
    const result = await api.submit({
      threadId: origin.kind === "thread" ? origin.id : undefined,
      mode: origin.kind === "new" ? origin.mode ?? undefined : undefined,
      text,
      attachment,
      attachmentRole,
      clientRequestId: requestIdFor(requestKey, [text, attachment, attachmentRole]),
    });
    // The server has the turn now; nothing below may report the message as unsent.
    requestIds.current.delete(requestKey);
    drafts.current.delete(key);
    void refreshThreads();
    // The user moved on while this was sending: leave them where they are.
    if (viewRef.current !== origin) return;
    let target = origin;
    if (origin.kind === "new") {
      target = { kind: "thread", id: result.thread_id };
      enter(target, "replace");
    }
    adoptGeneration({ id: result.generation_id, thread_id: result.thread_id, status: "queued", blocks: [], seq: 0 });
    if (!(await refreshDetail(result.thread_id)) && viewRef.current === target) {
      setNotice("Your message was sent, but the conversation could not be refreshed. Reload the page if it does not update.");
    }
  }

  async function retry(failed: GenerationSnapshot) {
    const origin = viewRef.current;
    const requestKey = `retry:${failed.id}`;
    const result = await api.retry(failed.id, requestIdFor(requestKey, []));
    requestIds.current.delete(requestKey);
    if (viewRef.current !== origin) return;
    adoptGeneration({ id: result.generation_id, thread_id: failed.thread_id, status: "queued", blocks: [], seq: 0 });
  }

  function openThread(id: string) {
    const shown = viewRef.current;
    if (shown.kind === "thread" && shown.id === id && detailRef.current?.id === id) {
      setSidebarOpen(false);
      return;
    }
    openView({ kind: "thread", id }, "push");
  }

  function closeDelete() {
    setDeleteTarget(null);
    setDeletion({ busy: false, error: "" });
  }

  async function removeThread() {
    const target = deleteTarget;
    if (!target || deletion.busy) return;
    setDeletion({ busy: true, error: "" });
    try {
      await api.deleteThread(target.id);
    } catch (reason) {
      // Already deleted (for example in another tab) is the outcome the user asked for.
      if (!(reason instanceof ApiError && reason.status === 404)) {
        if (!isUnauthorized(reason)) setDeletion({ busy: false, error: errorMessage(reason, "The conversation could not be deleted. Try again.") });
        return;
      }
    }
    closeDelete();
    drafts.current.delete(viewKey({ kind: "thread", id: target.id }));
    const shown = viewRef.current;
    if (shown.kind === "thread" && shown.id === target.id) enter({ kind: "new", mode: null }, "replace");
    void refreshThreads();
  }

  async function createHandoff(conversation: ThreadDetail) {
    const run = ++handoffRun.current;
    const signal = lifetime.current;
    const running = () => handoffRun.current === run && !signal?.aborted;
    const threadId = conversation.id;
    const requestKey = `handoff:${threadId}`;
    // A handoff still running from before (for example across a reload) is followed
    // instead of starting a second one, which the server would refuse.
    let generationId = conversation.handoff && !isTerminal(conversation.handoff.status) ? conversation.handoff.id : null;
    setHandoff({ threadId, generationId, blocks: [], finished: false });
    try {
      if (!generationId) {
        generationId = (await api.handoff(threadId, requestIdFor(requestKey, []))).generation_id;
        requestIds.current.delete(requestKey);
        if (!running()) {
          // Closed while it was being created.
          void api.stop(generationId).catch(() => undefined);
          return;
        }
        setHandoff({ threadId, generationId, blocks: [], finished: false });
      }
      let snapshot = await api.generation(generationId);
      while (running()) {
        const finished = isTerminal(snapshot.status);
        setHandoff({ threadId, generationId, blocks: snapshot.blocks, finished });
        if (finished) break;
        await delay(HANDOFF_POLL_MS);
        if (!running()) return;
        snapshot = await api.generation(generationId);
      }
      if (!running()) return;
      setDetail((shown) => (shown?.id === threadId && shown.handoff ? { ...shown, handoff: null } : shown));
      if (snapshot.status !== "completed") setGlobalError(snapshot.error_message ?? "Prompt handoff could not be created.");
      refreshUsage();
    } catch (reason) {
      if (!running()) return;
      setHandoff(null);
      if (!isUnauthorized(reason)) setGlobalError(errorMessage(reason, "Prompt handoff could not be created."));
    }
  }

  function closeHandoff() {
    handoffRun.current += 1;
    const closing = handoff;
    setHandoff(null);
    if (!closing?.generationId || closing.finished) return;
    // A handoff is shown only in this dialog: left running, it would be paid for without
    // ever being seen and would keep the conversation busy. The dialog says so.
    const generationId = closing.generationId;
    void api.stop(generationId).catch(() => undefined);
    setDetail((shown) => (shown?.handoff?.id === generationId ? { ...shown, handoff: null } : shown));
  }

  async function logout() {
    if (signingOut) return;
    setSigningOut(true);
    try {
      await api.logout();
      onSignedOut();
    } catch (reason) {
      // 401: the session had already ended, and the app has switched to sign-in.
      if (isUnauthorized(reason)) return;
      setSigningOut(false);
      setGlobalError(errorMessage(reason, "Sign-out failed. Check your connection and try again."));
    }
  }

  const composerShown = view.kind === "thread" ? Boolean(current) : view.mode !== null;
  const draftKey = viewKey(view);

  return (
    <div className="app-shell">
      <button className="mobile-menu" onClick={() => setSidebarOpen(true)} aria-label="Open sidebar" aria-expanded={sidebarOpen} aria-controls={sidebarId}><Menu /></button>
      <aside id={sidebarId} className={sidebarOpen ? "sidebar open" : "sidebar"} inert={mobileLayout && !sidebarOpen}>
        <SidebarHeader onClose={() => setSidebarOpen(false)} />
        <button className="new-button" onClick={() => openView({ kind: "new", mode: null }, "push")}><Plus /> New</button>
        <nav className="thread-list" aria-label="Conversations">
          {threads.map((thread) => {
            const selected = view.kind === "thread" && view.id === thread.id;
            return (
              <div className={selected ? "thread-row selected" : "thread-row"} key={thread.id}>
                <button className="thread-item" onClick={() => openThread(thread.id)} aria-current={selected ? "page" : undefined}>
                  <div><span className="thread-title">{thread.title}</span>{thread.active_generation_id && <span className="activity-dot" role="img" aria-label="Generating" />}</div>
                  <ModeBadge mode={thread.mode} />
                </button>
                <button className="thread-delete" onClick={() => setDeleteTarget(thread)} aria-label={`Delete ${thread.title}`} title="Delete conversation"><Trash2 /></button>
              </div>
            );
          })}
        </nav>
        <footer className="sidebar-footer"><span className="cost">{usage}</span><button className="icon-button" onClick={() => void logout()} disabled={signingOut} aria-label="Sign out"><LogOut /></button></footer>
      </aside>
      <main className="workspace">
        {current?.docx_exportable && (
          <a className="docx-download" href={api.documentUrl(current.id)}>
            <Download aria-hidden="true" /> Download DOCX
          </a>
        )}
        <section className="conversation-area">
          {view.kind === "thread"
            ? current ? <MessageList detail={current} generation={active} /> : <div className="conversation-loading">Loading conversation…</div>
            : <EmptyThread mode={view.mode} onSelect={(mode) => openView({ kind: "new", mode }, "none")} />}
        </section>
        {composerShown && (
          <Composer
            key={draftKey}
            activeGeneration={active}
            readDraft={() => drafts.current.get(draftKey)}
            onDraftChange={(next) => drafts.current.set(draftKey, next)}
            onSend={(text, attachment, attachmentRole) => send(view, text, attachment, attachmentRole)}
            onStop={() => (active ? api.stop(active.id) : Promise.resolve())}
            onRetry={() => (active ? retry(active) : Promise.resolve())}
            onCreateHandoff={current ? () => void createHandoff(current) : undefined}
          />
        )}
      </main>
      {deleteTarget && (
        <Modal title="Delete conversation?" onClose={closeDelete}>
          <p className="modal-copy">Delete “{deleteTarget.title}”? This permanently deletes the conversation and its attachments. This cannot be undone.</p>
          {deletion.error && <p className="form-error" role="alert">{deletion.error}</p>}
          <div className="modal-actions">
            <button onClick={closeDelete}>Cancel</button>
            <button className="danger-button" onClick={() => void removeThread()} disabled={deletion.busy}>{deletion.busy ? "Deleting…" : "Delete permanently"}</button>
          </div>
        </Modal>
      )}
      {handoff !== null && (
        <Modal title="Prompt handoff" onClose={closeHandoff}>
          <div className="handoff-body">
            {handoff.blocks.length ? <ResponseBlocks blocks={handoff.blocks.map((block) => ({ ...block, type: "deliverable" }))} /> : <div className="handoff-loading">Collecting user instructions…</div>}
            {!handoff.finished && <p className="handoff-note">Closing this dialog cancels the handoff.</p>}
          </div>
        </Modal>
      )}
      <div className="toasts">
        {globalError && <div className="toast" role="alert">{globalError}<button onClick={() => setGlobalError("")} aria-label="Dismiss"><X /></button></div>}
        {notice && <div className="toast notice" role="status">{notice}<button onClick={() => setNotice("")} aria-label="Dismiss notice"><X /></button></div>}
      </div>
      <div className="visually-hidden" role="status" aria-live="polite">{announcement}</div>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<{ user: SessionUser; key: number } | null | undefined>(undefined);
  const signIns = useRef(0);

  const bootstrap = useCallback(async () => {
    try {
      const current = await api.me();
      if (current.csrf_token) setCsrfToken(current.csrf_token);
      signIns.current += 1;
      setSession({ user: current, key: signIns.current });
    } catch {
      setSession(null);
    }
  }, []);
  useEffect(() => { void bootstrap(); }, [bootstrap]);
  useEffect(() => {
    const unauthorized = () => setSession(null);
    window.addEventListener("oveo:unauthorized", unauthorized);
    return () => window.removeEventListener("oveo:unauthorized", unauthorized);
  }, []);
  useEffect(() => {
    if (session !== null) return;
    const normalizeAddress = () => updateAddress(canonicalPath(window.location.pathname, false), true);
    normalizeAddress();
    window.addEventListener("popstate", normalizeAddress);
    return () => window.removeEventListener("popstate", normalizeAddress);
  }, [session]);

  if (session === undefined) return <div className="app-loading"><BrandLogo /></div>;
  if (!session) return <Login onLogin={() => void bootstrap()} />;
  return <Workspace key={session.key} user={session.user} onSignedOut={() => setSession(null)} />;
}
