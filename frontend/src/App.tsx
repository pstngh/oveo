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
import { type FormEvent, type KeyboardEvent, type ReactNode, memo, useCallback, useEffect, useId, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, ApiError, NO_ANSWER, setCsrfToken } from "./api";
import BrandLogo from "./BrandLogo";
import { followGeneration, isTerminal } from "./stream";
import type {
  AttachmentRole,
  ContentBlock,
  GenerationSnapshot,
  Message,
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
// The history follows new text only while the reader is this close to its end.
const FOLLOW_THRESHOLD_PX = 80;
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
    guidance: "Share the brief, known facts, audience, desired locale (or both English and French), and any practical constraints. Oveo will not invent missing details.",
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

/** What a send reports when it failed: the server's reason, or that the outcome is unknown. */
function sendFailure(reason: unknown) {
  // Without an answer from Oveo (network failure, proxy error page) the message may or
  // may not have arrived. Sending it again reuses the same request identifier.
  return reason instanceof ApiError && reason.code !== NO_ANSWER ? reason.message : UNCONFIRMED_SEND;
}

/** A generation the server has just accepted, shown until its first snapshot arrives. */
function queued(id: string, threadId: string | null): GenerationSnapshot {
  return { id, thread_id: threadId, status: "queued", blocks: [], error_code: null, error_message: null, retryable: false, seq: 0 };
}

/** Save a downloaded file under the name the server gave it. */
function saveFile(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  // Some browsers read the object URL after the click has returned.
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
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

/** The newest message shown (messages arrive in order). */
function lastOrdinal(detail: ThreadDetail) {
  return detail.messages.at(-1)?.ordinal ?? 0;
}

/** Add the messages of an incremental refresh to the conversation already shown. */
export function mergeDetail(previous: ThreadDetail | undefined, loaded: ThreadDetail, incremental: boolean): ThreadDetail {
  if (!incremental || !previous || previous.id !== loaded.id) return loaded;
  const shown = new Set(previous.messages.map((message) => message.id));
  return { ...loaded, messages: [...previous.messages, ...loaded.messages.filter((message) => !shown.has(message.id))] };
}

/** A refresh can return an older copy of the generation the stream already advanced. */
function newerGeneration(previous: GenerationSnapshot | null, loaded: GenerationSnapshot | null) {
  if (!previous || !loaded || previous.id !== loaded.id) return loaded;
  // A finished generation never runs again, so a finished copy wins. The sequence
  // numbers of a live draft and of a stored row are not comparable.
  const finished = isTerminal(previous.status);
  if (finished !== isTerminal(loaded.status)) return finished ? previous : loaded;
  return previous.seq > loaded.seq ? previous : loaded;
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
      onLogin(await api.login(username.trim(), password));
    } catch (reason) {
      setError(errorMessage(reason, "Sign-in failed. Please try again."));
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

// Stable options, so a memoized message is not parsed again on every render.
const MARKDOWN_PLUGINS = [remarkGfm];
const MARKDOWN_ELEMENTS = ["p", "strong", "em", "ul", "ol", "li", "a", "code", "blockquote", "br"];
const MARKDOWN_COMPONENTS: Components = {
  a: ({ href, children }) => <a href={href} rel="noreferrer" target="_blank">{children}</a>,
};

const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={MARKDOWN_PLUGINS}
      skipHtml
      allowedElements={MARKDOWN_ELEMENTS}
      unwrapDisallowed
      components={MARKDOWN_COMPONENTS}
    >
      {text}
    </ReactMarkdown>
  );
});

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

// Memoized: a streamed delta re-renders only the pending answer, not every earlier
// message (whose Markdown would otherwise be parsed again many times a second).
const MessageItem = memo(function MessageItem({ message }: { message: Message }) {
  return (
    <article className={`message ${message.role}`}>
      <div className="message-inner">
        {message.role === "assistant" ? <ResponseBlocks blocks={message.blocks} /> : (
          <>
            {message.blocks.map((block, index) => <p className="user-text" key={index}>{block.text}</p>)}
            {message.attachment && <div className="sent-attachment"><FileText /> <span>{message.attachment.filename}</span><small>{message.attachment.role === "reference" ? "Reference" : "Source"}</small></div>}
          </>
        )}
      </div>
    </article>
  );
});

// Memoized: typing in the message box re-renders the workspace, not the conversation.
export const MessageList = memo(function MessageList({ detail, generation }: { detail: ThreadDetail; generation: GenerationSnapshot | null }) {
  const messagesRef = useRef<HTMLDivElement>(null);
  // Whether the view follows new text: a reader who scrolled up to read earlier text is
  // not pulled back down by the streaming answer, nor when that answer is then stored as
  // a message. The user's own new message, and a newly opened conversation, are shown.
  const following = useRef(true);
  const shown = useRef({ thread: detail.id, count: detail.messages.length });
  const messageCount = detail.messages.length;
  useEffect(() => {
    const previous = shown.current;
    shown.current = { thread: detail.id, count: detail.messages.length };
    if (previous.thread !== detail.id || detail.messages.slice(previous.count).some((message) => message.role === "user")) {
      following.current = true;
    }
  }, [detail]);
  useEffect(() => {
    const messages = messagesRef.current;
    if (messages && following.current) messages.scrollTop = messages.scrollHeight;
  }, [messageCount, generation?.seq]);
  function trackPosition() {
    const messages = messagesRef.current;
    if (messages) following.current = messages.scrollHeight - messages.scrollTop - messages.clientHeight < FOLLOW_THRESHOLD_PX;
  }
  // Not a live region: streamed text would be read out chunk by chunk. The workspace
  // announces coarse status changes in its own status region instead.
  return (
    <div ref={messagesRef} className="messages" onScroll={trackPosition}>
      {detail.messages.map((message) => <MessageItem key={message.id} message={message} />)}
      {generation && !isTerminal(generation.status) && (
        <article className="message assistant pending"><div className="message-inner"><ResponseBlocks blocks={generation.blocks} streaming /></div></article>
      )}
      {generation?.status === "completed" && generation.blocks.length > 0 && (
        // The finished answer stays in view until the refreshed conversation holds it
        // as a message (the refresh then clears the generation in the same render).
        <article className="message assistant"><div className="message-inner"><ResponseBlocks blocks={generation.blocks} /></div></article>
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
});

export interface ComposerDraft {
  text: string;
  attachment?: File;
  attachmentRole: AttachmentRole;
}

export const EMPTY_DRAFT: ComposerDraft = { text: "", attachmentRole: "source" };

/**
 * The message box of one conversation. Its draft belongs to the workspace, so leaving
 * and coming back (even while it is being sent) shows the same draft in the same state.
 */
export function Composer({
  activeGeneration,
  draft,
  sending,
  onDraftChange,
  onSend,
  onStop,
  onRetry,
  onCreateHandoff,
}: {
  activeGeneration: GenerationSnapshot | null;
  draft: ComposerDraft;
  /** The draft is being sent: it stays frozen until the server answers. */
  sending: boolean;
  onDraftChange: (draft: ComposerDraft) => void;
  onSend: (draft: ComposerDraft) => Promise<void>;
  onStop: () => Promise<void>;
  onRetry: () => Promise<void>;
  onCreateHandoff?: () => void;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<"stop" | "retry" | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const { text, attachment, attachmentRole } = draft;
  const generating = Boolean(activeGeneration && !isTerminal(activeGeneration.status));
  const retryable = Boolean(
    activeGeneration && ["failed", "stopped"].includes(activeGeneration.status) && activeGeneration.retryable,
  );

  function change(next: Partial<ComposerDraft>) {
    onDraftChange({ ...draft, ...next });
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
    if ((!text.trim() && !attachment) || sending || busy || generating) return;
    setError("");
    try {
      await onSend(draft);
      if (fileRef.current) fileRef.current.value = "";
    } catch (reason) {
      // The text stays here, so it can be sent again.
      setError(sendFailure(reason));
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
    // Enter that confirms an input-method composition is not a send. Safari ends the
    // composition first and reports that Enter with keyCode 229 instead of isComposing.
    const composing = event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229;
    if (event.key === "Enter" && !event.shiftKey && !composing) {
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
        {attachment && <div className="attachment-chip"><FileText /><span>{attachment.name}</span><select aria-label="Use attachment as" value={attachmentRole} disabled={sending} onChange={(event) => change({ attachmentRole: event.target.value as AttachmentRole })}><option value="source">Source document</option><option value="reference">Style reference</option></select><button onClick={removeAttachment} disabled={sending} aria-label="Remove attachment"><X /></button></div>}
        <textarea
          aria-label="Message"
          placeholder="Message"
          value={text}
          onChange={(event) => change({ text: event.target.value })}
          onKeyDown={keyDown}
          rows={3}
          disabled={generating}
          readOnly={sending}
        />
        <div className="composer-actions">
          {onCreateHandoff && <button className="icon-button handoff-button" onClick={onCreateHandoff} disabled={generating} aria-label="Create prompt handoff" title="Create prompt handoff"><Sparkles /></button>}
          <button className="icon-button attach" onClick={() => fileRef.current?.click()} disabled={generating || sending} aria-label="Attach a DOCX file"><Paperclip /></button>
          <input ref={fileRef} className="file-input" type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={(e) => acceptFile(e.target.files?.[0])} />
          {generating ? (
            // Enabled while stopping too: a second Stop finishes a stop the server lost.
            <button className="send-button stop" onClick={() => void run("stop", onStop, "The response could not be stopped. Try again.")} disabled={busy === "stop"} aria-label="Stop generation">
              <Square />
            </button>
          ) : (
            <button className="send-button" onClick={() => void send()} disabled={sending || busy !== null || (!text.trim() && !attachment)} aria-label="Send message"><Send /></button>
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

// Memoized: typing in the message box re-renders the workspace, and a long list of
// conversations would otherwise be rebuilt on every keystroke.
const ThreadList = memo(function ThreadList({
  threads,
  selectedId,
  onOpen,
  onDelete,
}: {
  threads: ThreadSummary[];
  selectedId: string | null;
  onOpen: (id: string) => void;
  onDelete: (thread: ThreadSummary) => void;
}) {
  return (
    <nav className="thread-list" aria-label="Conversations">
      {threads.map((thread) => {
        const selected = thread.id === selectedId;
        return (
          <div className={selected ? "thread-row selected" : "thread-row"} key={thread.id}>
            <button className="thread-item" onClick={() => onOpen(thread.id)} aria-current={selected ? "page" : undefined}>
              <div><span className="thread-title">{thread.title}</span>{thread.active_generation_id && <span className="activity-dot" role="img" aria-label="Generating" />}</div>
              <ModeBadge mode={thread.mode} />
            </button>
            <button className="thread-delete" onClick={() => onDelete(thread)} aria-label={`Delete ${thread.title}`} title="Delete conversation"><Trash2 /></button>
          </div>
        );
      })}
    </nav>
  );
});

export function SidebarHeader({ onClose }: { onClose: () => void }) {
  return (
    <div className="sidebar-head">
      <button className="mobile-close icon-button" onClick={onClose} aria-label="Close sidebar"><X /></button>
    </div>
  );
}

interface HandoffView {
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
  const [downloading, setDownloading] = useState(false);
  // Unsent drafts, and the ones being sent, by conversation (see viewKey).
  const [drafts, setDrafts] = useState<ReadonlyMap<string, ComposerDraft>>(() => new Map());
  const [sending, setSending] = useState<ReadonlySet<string>>(() => new Set());
  const sidebarId = useId();

  // Every navigation stores a new view object here, so a response that arrives for a
  // view the user has left can tell and is dropped.
  const viewRef = useRef(view);
  const detailRef = useRef(detail);
  // Changes whenever the shown generation is replaced by navigation, a send or a retry,
  // so an older refresh cannot put back the generation it read.
  const generationEpoch = useRef(0);
  const listRequest = useRef(0);
  // Conversation loads and refreshes, newest last: an older response that arrives late
  // must not replace what a newer one showed.
  const detailRequest = useRef(0);
  const handoffRun = useRef(0);
  const stopHandoffFollow = useRef<(() => void) | null>(null);
  // Set at sign-out: a response that arrives afterwards must change nothing, not even
  // the address.
  const closed = useRef(false);
  const requestIds = useRef(new Map<string, { id: string; parts: unknown[] }>());

  useEffect(() => { detailRef.current = detail; }, [detail]);
  useEffect(() => {
    closed.current = false;
    return () => {
      closed.current = true;
      stopHandoffFollow.current?.();
      document.title = "Oveo";
    };
  }, []);

  const setDraft = useCallback((key: string, draft: ComposerDraft | null) => {
    setDrafts((previous) => {
      const next = new Map(previous);
      if (draft) next.set(key, draft);
      else next.delete(key);
      return next;
    });
  }, []);

  const markSending = useCallback((key: string, active: boolean) => {
    setSending((previous) => {
      const next = new Set(previous);
      if (active) next.add(key);
      else next.delete(key);
      return next;
    });
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
    if (closed.current) return;
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
    const request = ++detailRequest.current;
    void api.thread(next.id).then(
      (loaded) => {
        if (viewRef.current !== next || detailRequest.current !== request) return;
        setDetail(loaded);
        setGeneration(loaded.generation);
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
    const after = shown ? lastOrdinal(shown) : undefined;
    const epoch = generationEpoch.current;
    const request = ++detailRequest.current;
    try {
      const loaded = await api.thread(id, after);
      // A newer load or refresh answers for this view; this older copy could undo it.
      if (viewRef.current !== target || detailRequest.current !== request) return true;
      setDetail((previous) => mergeDetail(previous, loaded, after !== undefined));
      if (generationEpoch.current === epoch) {
        setGeneration((previous) => newerGeneration(previous, loaded.generation));
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

  /** A turn was refused because another one is running (for example from another tab): show that one. */
  function showRunningTurn(reason: unknown, threadId: string | null) {
    if (threadId && reason instanceof ApiError && reason.status === 409) void refreshDetail(threadId);
  }

  async function send(origin: View, draft: ComposerDraft) {
    const key = viewKey(origin);
    const requestKey = `send:${key}`;
    const { text, attachment, attachmentRole } = draft;
    markSending(key, true);
    let result: { thread_id: string; generation_id: string };
    try {
      result = await api.submit({
        threadId: origin.kind === "thread" ? origin.id : undefined,
        mode: origin.kind === "new" ? origin.mode ?? undefined : undefined,
        text,
        attachment,
        attachmentRole,
        clientRequestId: requestIdFor(requestKey, [text, attachment, attachmentRole]),
      });
    } catch (reason) {
      showRunningTurn(reason, origin.kind === "thread" ? origin.id : null);
      throw reason;
    } finally {
      markSending(key, false);
    }
    // The server has the turn now; nothing below may report the message as unsent.
    requestIds.current.delete(requestKey);
    setDraft(key, null);
    if (closed.current) return;
    void refreshThreads();
    // The user moved on while this was sending: leave them where they are. Coming back
    // to the same conversation counts as being there.
    if (viewKey(viewRef.current) !== key) return;
    if (origin.kind === "new") enter({ kind: "thread", id: result.thread_id }, "replace");
    const target = viewRef.current;
    adoptGeneration(queued(result.generation_id, result.thread_id));
    if (!(await refreshDetail(result.thread_id)) && viewRef.current === target) {
      setNotice("Your message was sent, but the conversation could not be refreshed. Reload the page if it does not update.");
    }
  }

  async function retry(failed: GenerationSnapshot) {
    const origin = viewRef.current;
    const requestKey = `retry:${failed.id}`;
    let result: { generation_id: string };
    try {
      result = await api.retry(failed.id, requestIdFor(requestKey, []));
    } catch (reason) {
      showRunningTurn(reason, failed.thread_id);
      throw reason;
    }
    requestIds.current.delete(requestKey);
    if (viewRef.current !== origin) return;
    adoptGeneration(queued(result.generation_id, failed.thread_id));
  }

  const openThread = useCallback((id: string) => {
    const shown = viewRef.current;
    if (shown.kind === "thread" && shown.id === id && detailRef.current?.id === id) {
      setSidebarOpen(false);
      return;
    }
    openView({ kind: "thread", id }, "push");
  }, [openView]);

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
    setDraft(viewKey({ kind: "thread", id: target.id }), null);
    const shown = viewRef.current;
    if (shown.kind === "thread" && shown.id === target.id) enter({ kind: "new", mode: null }, "replace");
    void refreshThreads();
  }

  function clearShownHandoff(generationId: string) {
    setDetail((shown) => (shown?.handoff?.id === generationId ? { ...shown, handoff: null } : shown));
  }

  function stopFollowingHandoff() {
    stopHandoffFollow.current?.();
    stopHandoffFollow.current = null;
  }

  async function createHandoff(conversation: ThreadDetail) {
    stopFollowingHandoff();
    const run = ++handoffRun.current;
    const running = () => handoffRun.current === run && !closed.current;
    // A handoff still running from before (for example across a reload) is followed
    // instead of starting a second one, which the server would refuse.
    let generationId = conversation.handoff && !isTerminal(conversation.handoff.status) ? conversation.handoff.id : null;
    setHandoff({ generationId, blocks: [], finished: false });
    if (!generationId) {
      const requestKey = `handoff:${conversation.id}`;
      try {
        // A handoff requested after new turns is a new request, even if an earlier
        // attempt's outcome was unknown.
        generationId = (await api.handoff(conversation.id, requestIdFor(requestKey, [lastOrdinal(conversation)]))).generation_id;
      } catch (reason) {
        if (!running()) return;
        setHandoff(null);
        if (!isUnauthorized(reason)) setGlobalError(errorMessage(reason, "Prompt handoff could not be created."));
        return;
      }
      requestIds.current.delete(requestKey);
      if (!running()) {
        // Closed while it was being created.
        void api.stop(generationId).catch(() => undefined);
        return;
      }
      setHandoff({ generationId, blocks: [], finished: false });
    }
    const followed = generationId;
    stopHandoffFollow.current = followGeneration(followed, {
      onUpdate: (snapshot) => {
        const finished = isTerminal(snapshot.status);
        setHandoff({ generationId: followed, blocks: snapshot.blocks, finished });
        if (!finished) return;
        clearShownHandoff(followed);
        if (snapshot.status !== "completed") setGlobalError(snapshot.error_message ?? "Prompt handoff could not be created.");
        refreshUsage();
      },
      onUnavailable: () => {
        // Signed out, or its conversation was deleted: nothing is left to follow or stop.
        stopFollowingHandoff();
        setHandoff(null);
        clearShownHandoff(followed);
        setGlobalError("The prompt handoff is no longer available.");
      },
    });
  }

  function closeHandoff() {
    handoffRun.current += 1;
    stopFollowingHandoff();
    const closing = handoff;
    setHandoff(null);
    if (!closing?.generationId || closing.finished) return;
    // A handoff is shown only in this dialog: left running, it would be paid for without
    // ever being seen and would keep the conversation busy. The dialog says so.
    const generationId = closing.generationId;
    void api.stop(generationId).catch(() => undefined);
    clearShownHandoff(generationId);
  }

  async function downloadDocument(conversation: ThreadDetail) {
    if (downloading) return;
    setDownloading(true);
    try {
      // Fetched rather than followed as a link: a refused export (busy, outdated) is
      // reported here instead of replacing the app, and its unsent drafts, with an error.
      const { blob, filename } = await api.document(conversation.id);
      saveFile(blob, filename);
    } catch (reason) {
      if (!isUnauthorized(reason)) setGlobalError(errorMessage(reason, "The Word document could not be downloaded. Try again."));
    } finally {
      setDownloading(false);
    }
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
        <ThreadList threads={threads} selectedId={view.kind === "thread" ? view.id : null} onOpen={openThread} onDelete={setDeleteTarget} />
        <footer className="sidebar-footer"><span className="cost">{usage}</span><button className="icon-button" onClick={() => void logout()} disabled={signingOut} aria-label="Sign out"><LogOut /></button></footer>
      </aside>
      <main className="workspace">
        {current?.docx_exportable && (
          <button className="docx-download" onClick={() => void downloadDocument(current)} disabled={downloading}>
            <Download aria-hidden="true" /> {downloading ? "Preparing DOCX…" : "Download DOCX"}
          </button>
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
            draft={drafts.get(draftKey) ?? EMPTY_DRAFT}
            sending={sending.has(draftKey)}
            onDraftChange={(next) => setDraft(draftKey, next)}
            onSend={(next) => send(view, next)}
            onStop={() => (active ? api.stop(active.id) : Promise.resolve())}
            onRetry={() => (active ? retry(active) : Promise.resolve())}
            onCreateHandoff={current ? () => void createHandoff(current) : undefined}
          />
        )}
      </main>
      {deleteTarget && (
        <Modal title="Delete conversation?" onClose={() => { if (!deletion.busy) closeDelete(); }}>
          <p className="modal-copy">Delete “{deleteTarget.title}”? This permanently deletes the conversation and its attachments. This cannot be undone.</p>
          {deletion.error && <p className="form-error" role="alert">{deletion.error}</p>}
          <div className="modal-actions">
            <button onClick={closeDelete} disabled={deletion.busy}>Cancel</button>
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
  // undefined while the session is being checked; null when signed out.
  const [session, setSession] = useState<{ user: SessionUser; key: number } | null | undefined>(undefined);
  const [unreachable, setUnreachable] = useState(false);
  const signIns = useRef(0);

  const start = useCallback((user: SessionUser) => {
    if (user.csrf_token) setCsrfToken(user.csrf_token);
    signIns.current += 1;
    setSession({ user, key: signIns.current });
  }, []);
  const bootstrap = useCallback(async () => {
    setUnreachable(false);
    try {
      start(await api.me());
    } catch (reason) {
      // Only a refused session means signed out. Anything else (offline, a restart in
      // progress) must not ask for the password again and start a second session.
      if (isUnauthorized(reason)) setSession(null);
      else setUnreachable(true);
    }
  }, [start]);
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

  if (unreachable) {
    return (
      <main className="login-page">
        <div className="login-card">
          <BrandLogo />
          <p className="form-error" role="alert">Oveo could not be reached. Check your connection and try again.</p>
          <button className="primary-button" onClick={() => void bootstrap()}>Try again</button>
        </div>
      </main>
    );
  }
  if (session === undefined) return <div className="app-loading"><BrandLogo /></div>;
  if (!session) return <Login onLogin={start} />;
  return <Workspace key={session.key} user={session.user} onSignedOut={() => setSession(null)} />;
}
