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
import type {
  ContentBlock,
  GenerationSnapshot,
  Mode,
  SessionUser,
  ThreadDetail,
  ThreadSummary,
} from "./types";

const uuid = () => crypto.randomUUID();

const MODE_COPY: Record<Mode, { label: string; heading: string; guidance: string }> = {
  translate: {
    label: "Translate",
    heading: "What would you like to translate?",
    guidance: "Share the French or English source text. If an English source has no French target yet, Oveo will ask only which French variety you want.",
  },
  revision: {
    label: "Revision",
    heading: "What would you like to revise?",
    guidance: "Share the existing text and, when it matters, the depth you want: proofread, copyedit, revise, or rewrite.",
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
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return null;
  }
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
  const [copied, setCopied] = useState<number | null>(null);
  const visibleBlocks = streaming ? blocks.filter((block) => block.text.trim().length > 0) : blocks;
  async function copy(text: string, index: number) {
    await navigator.clipboard.writeText(text);
    setCopied(index);
    window.setTimeout(() => setCopied(null), 1600);
  }
  return (
    <div className={`response-blocks${streaming ? " streaming" : ""}`}>
      {visibleBlocks.map((block, index) => block.type === "deliverable" ? (
        <section className="deliverable" key={index} aria-label="Deliverable">
          <button
            className="copy-button"
            onClick={() => copy(block.text, index)}
            aria-label={copied === index ? "Copied" : "Copy deliverable"}
            title={copied === index ? "Copied" : "Copy deliverable"}
          >
            {copied === index ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          </button>
          <pre>{block.text}</pre>
        </section>
      ) : (
        <section className={block.type} key={index}>
          {block.type === "advice" && <h3>Notes</h3>}
          <Markdown text={block.text} />
        </section>
      ))}
      {streaming && <span className="stream-caret" aria-label="Generating" />}
    </div>
  );
}

export function MessageList({ detail, generation }: { detail: ThreadDetail; generation: GenerationSnapshot | null }) {
  const messagesRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const messages = messagesRef.current;
    if (messages) messages.scrollTop = messages.scrollHeight;
  }, [detail.messages.length, generation?.seq]);
  return (
    <div ref={messagesRef} className="messages" aria-live="polite">
      {detail.messages.map((message) => (
        <article className={`message ${message.role}`} key={message.id}>
          <div className="message-inner">
            {message.role === "assistant" ? <ResponseBlocks blocks={message.blocks} /> : (
              <>
                {message.actor_username && message.actor_username !== detail.owner_username && <div className="message-author">{message.actor_username}</div>}
                {message.blocks.map((block, index) => <p className="user-text" key={index}>{block.text}</p>)}
                {message.attachment && <div className="sent-attachment"><FileText /> <span>{message.attachment.filename}</span></div>}
              </>
            )}
          </div>
        </article>
      ))}
      {generation && ["queued", "running", "stopping"].includes(generation.status) && (
        <article className="message assistant pending"><div className="message-inner"><ResponseBlocks blocks={generation.blocks} streaming /></div></article>
      )}
      {generation?.status === "failed" && generation.blocks.length > 0 && (
        <article className="message assistant preserved"><div className="message-inner"><ResponseBlocks blocks={generation.blocks} /></div></article>
      )}
      {generation && ["failed", "stopped"].includes(generation.status) && (
        <div className="generation-notice" role="status">
          <span>{generation.status === "stopped" ? "Generation stopped." : generation.error_message ?? "Oveo could not complete this response."}</span>
        </div>
      )}
    </div>
  );
}

export function Composer({
  activeGeneration,
  onSend,
  onStop,
  onRetry,
  onCreateHandoff,
}: {
  activeGeneration: GenerationSnapshot | null;
  onSend: (text: string, attachment?: File) => Promise<void>;
  onStop: () => Promise<void>;
  onRetry: () => Promise<void>;
  onCreateHandoff?: () => void;
}) {
  const [text, setText] = useState("");
  const [attachment, setAttachment] = useState<File>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const generating = activeGeneration && ["queued", "running", "stopping"].includes(activeGeneration.status);
  const retryable = activeGeneration && ["failed", "stopped"].includes(activeGeneration.status);

  function acceptFile(file?: File) {
    if (!file) return;
    if (attachment) return setError("Remove the current attachment before adding another.");
    if (!/\.docx$/i.test(file.name)) return setError("Only .docx files are supported.");
    setAttachment(file);
    setError("");
  }
  async function send() {
    if ((!text.trim() && !attachment) || busy || generating) return;
    setBusy(true);
    setError("");
    try {
      await onSend(text, attachment);
      setText("");
      setAttachment(undefined);
      if (fileRef.current) fileRef.current.value = "";
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "Message could not be sent.");
    } finally {
      setBusy(false);
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
      {retryable && <button className="retry-button" onClick={onRetry}><RotateCcw /> Retry</button>}
      <div className="composer">
        {attachment && <div className="attachment-chip"><FileText /><span>{attachment.name}</span><button onClick={() => setAttachment(undefined)} aria-label="Remove attachment"><X /></button></div>}
        <textarea
          aria-label="Message"
          placeholder="Message"
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={keyDown}
          rows={3}
          disabled={Boolean(generating)}
        />
        <div className="composer-actions">
          {onCreateHandoff && <button className="icon-button handoff-button" onClick={onCreateHandoff} disabled={Boolean(generating)} aria-label="Create prompt handoff" title="Create prompt handoff"><Sparkles /></button>}
          <button className="icon-button attach" onClick={() => fileRef.current?.click()} disabled={Boolean(generating)} aria-label="Attach a DOCX file"><Paperclip /></button>
          <input ref={fileRef} className="file-input" type="file" accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document" onChange={(e) => acceptFile(e.target.files?.[0])} />
          {generating ? (
            <button className="send-button stop" onClick={onStop} aria-label="Stop generation"><Square /></button>
          ) : (
            <button className="send-button" onClick={send} disabled={busy || (!text.trim() && !attachment)} aria-label="Send message"><Send /></button>
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

export default function App() {
  const [user, setUser] = useState<SessionUser | null | undefined>(undefined);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [detail, setDetail] = useState<ThreadDetail>();
  const [draftMode, setDraftMode] = useState<Mode | null>(null);
  const [generation, setGeneration] = useState<GenerationSnapshot | null>(null);
  const [usage, setUsage] = useState("$0.00");
  const [deleteTarget, setDeleteTarget] = useState<ThreadSummary | null>(null);
  const [handoff, setHandoff] = useState<ContentBlock[] | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [globalError, setGlobalError] = useState("");

  const refreshUsage = useCallback(() => {
    void api.usage().then((total) => setUsage(total.formatted)).catch(() => undefined);
  }, []);
  const bootstrap = useCallback(async () => {
    try {
      const current = await api.me();
      if (current.csrf_token) setCsrfToken(current.csrf_token);
      setUser(current);
      refreshUsage();
    } catch {
      setUser(null);
    }
  }, [refreshUsage]);
  useEffect(() => { void bootstrap(); }, [bootstrap]);
  useEffect(() => {
    const unauthorized = () => setUser(null);
    window.addEventListener("oveo:unauthorized", unauthorized);
    return () => window.removeEventListener("oveo:unauthorized", unauthorized);
  }, []);
  useEffect(() => {
    if (user !== null) return;
    const normalizeAddress = () => updateAddress(canonicalPath(window.location.pathname, false), true);
    normalizeAddress();
    window.addEventListener("popstate", normalizeAddress);
    return () => window.removeEventListener("popstate", normalizeAddress);
  }, [user]);

  const refreshThreads = useCallback(async () => {
    if (!user) return;
    try { setThreads(await api.threads()); } catch (reason) { setGlobalError(reason instanceof Error ? reason.message : "Could not load conversations."); }
  }, [user]);
  useEffect(() => {
    setDetail(undefined);
    setDraftMode(null);
    void refreshThreads();
  }, [refreshThreads]);
  useEffect(() => {
    if (!user) return;
    const timer = window.setInterval(() => void refreshThreads(), 5000);
    return () => window.clearInterval(timer);
  }, [user, refreshThreads]);

  const openThread = useCallback(async (id: string, navigate = true) => {
    setDraftMode(null);
    setSidebarOpen(false);
    const loaded = await api.thread(id);
    setDetail(loaded);
    setGeneration(loaded.generation ?? null);
    if (navigate) updateAddress(conversationPath(id));
  }, []);

  useEffect(() => {
    if (!user) return;
    const applyAddress = () => {
      const path = canonicalPath(window.location.pathname, true);
      updateAddress(path, true);
      const id = conversationIdFromPath(path);
      if (id) {
        void openThread(id, false).catch((reason) => {
          setGlobalError(reason instanceof Error ? reason.message : "Could not load that conversation.");
          setDetail(undefined);
          setGeneration(null);
          setDraftMode(null);
          updateAddress("/new", true);
        });
        return;
      }
      setDetail(undefined);
      setGeneration(null);
      setDraftMode(null);
      setSidebarOpen(false);
    };
    applyAddress();
    window.addEventListener("popstate", applyAddress);
    return () => window.removeEventListener("popstate", applyAddress);
  }, [openThread, user]);

  useEffect(() => {
    document.title = detail ? `${detail.title} · Oveo` : "Oveo";
  }, [detail]);

  const generationId = generation?.id;
  const generationStatus = generation?.status;
  useEffect(() => {
    if (!generationId || !generationStatus || !["queued", "running", "stopping"].includes(generationStatus)) return;
    const source = new EventSource(`/api/generations/${generationId}/events`);
    source.onmessage = (event) => {
      const next = JSON.parse(event.data) as GenerationSnapshot;
      setGeneration(next);
      if (["completed", "failed", "stopped"].includes(next.status)) {
        source.close();
        if (next.thread_id) void openThread(next.thread_id, false);
        void refreshThreads();
        refreshUsage();
      }
    };
    // EventSource reconnects automatically after a transient network interruption.
    source.onerror = () => undefined;
    return () => source.close();
  }, [generationId, generationStatus, openThread, refreshThreads, refreshUsage]);

  useEffect(() => {
    const context = (document as Document & { modelContext?: WebMcpContext }).modelContext;
    if (!context?.registerTool || !user) return;
    const lifecycle = new AbortController();
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
        setDetail(undefined);
        setGeneration(null);
        setDraftMode(value.mode);
        setSidebarOpen(false);
        updateAddress("/new");
        return { state: "ready", mode: value.mode, ownerUsername: user.username };
      },
    }, { signal: lifecycle.signal })).catch(() => undefined);
    return () => lifecycle.abort();
  }, [user]);

  async function send(text: string, attachment?: File) {
    if (!user) return;
    const result = await api.submit({
      threadId: detail?.id,
      mode: detail ? undefined : draftMode ?? undefined,
      text,
      attachment,
      clientRequestId: uuid(),
    });
    const [loaded, pending] = await Promise.all([api.thread(result.thread_id), api.generation(result.generation_id)]);
    setDetail(loaded);
    setDraftMode(null);
    setGeneration(pending);
    updateAddress(conversationPath(result.thread_id), true);
    await refreshThreads();
  }
  async function retry() {
    if (!generation) return;
    const result = await api.retry(generation.id, uuid());
    setGeneration(await api.generation(result.generation_id));
  }
  async function removeThread() {
    if (!deleteTarget) return;
    await api.deleteThread(deleteTarget.id);
    const deletedActiveThread = detail?.id === deleteTarget.id;
    setDeleteTarget(null);
    if (deletedActiveThread) {
      setDetail(undefined);
      setGeneration(null);
      updateAddress("/new", true);
    }
    await refreshThreads();
  }
  async function createHandoff() {
    if (!detail) return;
    setHandoff([]);
    try {
      const { generation_id } = await api.handoff(detail.id, uuid());
      let current = await api.generation(generation_id);
      setHandoff(current.blocks);
      while (["queued", "running", "stopping"].includes(current.status)) {
        await new Promise((resolve) => window.setTimeout(resolve, 700));
        current = await api.generation(generation_id);
        setHandoff(current.blocks);
      }
      if (current.status !== "completed") setGlobalError(current.error_message ?? "Prompt handoff could not be created.");
      refreshUsage();
    } catch (reason) {
      setHandoff(null);
      setGlobalError(reason instanceof Error ? reason.message : "Prompt handoff could not be created.");
    }
  }
  async function logout() {
    await api.logout();
    setUser(null);
  }
  function startNewConversation() {
    setDetail(undefined);
    setGeneration(null);
    setDraftMode(null);
    setSidebarOpen(false);
    updateAddress("/new");
  }
  if (user === undefined) return <div className="app-loading"><BrandLogo /></div>;
  if (!user) return <Login onLogin={() => void bootstrap()} />;
  const active = generation && generation.thread_id === detail?.id ? generation : null;

  return (
    <div className="app-shell">
      <button className="mobile-menu" onClick={() => setSidebarOpen(true)} aria-label="Open sidebar"><Menu /></button>
      <aside className={sidebarOpen ? "sidebar open" : "sidebar"}>
        <SidebarHeader onClose={() => setSidebarOpen(false)} />
        <button className="new-button" onClick={startNewConversation}><Plus /> New</button>
        <nav className="thread-list" aria-label="Conversations">
          {threads.map((thread) => (
            <div className={detail?.id === thread.id ? "thread-row selected" : "thread-row"} key={thread.id}>
              <button className="thread-item" onClick={() => void openThread(thread.id)}>
                <div><span className="thread-title">{thread.title}</span>{thread.active_generation_id && <span className="activity-dot" aria-label="Generating" />}</div>
                <ModeBadge mode={thread.mode} />
              </button>
              <button className="thread-delete" onClick={() => setDeleteTarget(thread)} aria-label={`Delete ${thread.title}`} title="Delete conversation"><Trash2 /></button>
            </div>
          ))}
        </nav>
        <footer className="sidebar-footer"><span className="cost">{usage}</span><button className="icon-button" onClick={logout} aria-label="Sign out"><LogOut /></button></footer>
      </aside>
      <main className="workspace">
        {detail?.docx_exportable && (
          <a className="docx-download" href={api.documentUrl(detail.id)}>
            <Download aria-hidden="true" /> Download DOCX
          </a>
        )}
        <section className="conversation-area">
          {detail ? <MessageList detail={detail} generation={active} /> : <EmptyThread mode={draftMode} onSelect={setDraftMode} />}
        </section>
        {(detail || draftMode) && <Composer activeGeneration={active} onSend={send} onStop={() => active ? api.stop(active.id) : Promise.resolve()} onRetry={retry} onCreateHandoff={detail ? () => void createHandoff() : undefined} />}
      </main>
      {deleteTarget && <Modal title="Delete conversation?" onClose={() => setDeleteTarget(null)}><p className="modal-copy">Delete “{deleteTarget.title}”? This permanently deletes the conversation and its attachments. This cannot be undone.</p><div className="modal-actions"><button onClick={() => setDeleteTarget(null)}>Cancel</button><button className="danger-button" onClick={removeThread}>Delete permanently</button></div></Modal>}
      {handoff !== null && <Modal title="Prompt handoff" onClose={() => setHandoff(null)}><div className="handoff-body">{handoff.length ? <ResponseBlocks blocks={handoff.map((block) => ({ ...block, type: "deliverable" }))} /> : <div className="handoff-loading">Collecting user instructions…</div>}</div></Modal>}
      {globalError && <div className="toast" role="alert">{globalError}<button onClick={() => setGlobalError("")} aria-label="Dismiss"><X /></button></div>}
    </div>
  );
}
