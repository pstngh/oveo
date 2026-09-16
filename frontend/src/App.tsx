import {
  Copy,
  FileText,
  LogOut,
  Menu,
  MessageSquareText,
  MoreHorizontal,
  Paperclip,
  PenLine,
  Plus,
  RotateCcw,
  Send,
  Sparkles,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { type FormEvent, type KeyboardEvent, type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, ApiError, setCsrfToken } from "./api";
import BrandLogo from "./BrandLogo";
import type {
  Account,
  ContentBlock,
  GenerationSnapshot,
  Mode,
  SessionUser,
  ThreadDetail,
  ThreadSummary,
} from "./types";

const uuid = () => crypto.randomUUID();

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

function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => closeRef.current?.focus(), []);
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
        <header>
          <h2 id="modal-title">{title}</h2>
          <button ref={closeRef} className="icon-button" onClick={onClose} aria-label="Close dialog"><X /></button>
        </header>
        {children}
      </section>
    </div>
  );
}

function Login({ onLogin }: { onLogin: (user: SessionUser) => void }) {
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
      <form className="login-card" onSubmit={submit}>
        <BrandLogo />
        <h1>Welcome back</h1>
        <p>Sign in to continue to Oveo.</p>
        <label>Username<input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        {error && <div className="form-error" role="alert">{error}</div>}
        <button className="primary-button" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </main>
  );
}

function ModeBadge({ mode }: { mode: Mode }) {
  return <span className={`mode-badge ${mode}`}>{mode === "translate" ? "Translate" : "AlithyaGPT"}</span>;
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
  async function copy(text: string, index: number) {
    await navigator.clipboard.writeText(text);
    setCopied(index);
    window.setTimeout(() => setCopied(null), 1600);
  }
  return (
    <div className={`response-blocks${streaming ? " streaming" : ""}`}>
      {blocks.map((block, index) => block.type === "deliverable" ? (
        <section className="deliverable" key={index} aria-label="Deliverable">
          <button className="copy-button" onClick={() => copy(block.text, index)} aria-label="Copy deliverable">
            <Copy /> {copied === index ? "Copied" : "Copy"}
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
}: {
  activeGeneration: GenerationSnapshot | null;
  onSend: (text: string, attachment?: File) => Promise<void>;
  onStop: () => Promise<void>;
  onRetry: () => Promise<void>;
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
    if (!file.name.toLowerCase().endsWith(".txt")) return setError("Only .txt files are supported.");
    setAttachment(file);
    setError("");
  }
  function pasted(event: React.ClipboardEvent<HTMLTextAreaElement>) {
    const pastedText = event.clipboardData.getData("text/plain");
    if (pastedText.length < 4000) return;
    event.preventDefault();
    if (attachment) return setError("Remove the current attachment before pasting another source.");
    acceptFile(new File([pastedText], "Pasted text.txt", { type: "text/plain;charset=utf-8" }));
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
          aria-label="Message Oveo"
          placeholder="Message Oveo"
          value={text}
          onChange={(event) => setText(event.target.value)}
          onPaste={pasted}
          onKeyDown={keyDown}
          rows={1}
          disabled={Boolean(generating)}
        />
        <div className="composer-actions">
          <button className="icon-button attach" onClick={() => fileRef.current?.click()} disabled={Boolean(generating)} aria-label="Attach a text file"><Paperclip /></button>
          <input ref={fileRef} className="file-input" type="file" accept=".txt,text/plain" onChange={(e) => acceptFile(e.target.files?.[0])} />
          {generating ? (
            <button className="send-button stop" onClick={onStop} aria-label="Stop generation"><Square /></button>
          ) : (
            <button className="send-button" onClick={send} disabled={busy || (!text.trim() && !attachment)} aria-label="Send message"><Send /></button>
          )}
        </div>
      </div>
      {error && <div className="composer-error" role="alert">{error}</div>}
      <div className="composer-hint">Enter to send · Shift+Enter for a new line</div>
    </div>
  );
}

export function EmptyThread({ mode, onSelect }: { mode: Mode | null; onSelect: (mode: Mode) => void }) {
  return (
    <div className="empty-thread">
      <div className="empty-mark"><BrandLogo compact /></div>
      <h1>{mode === "translate" ? "What would you like to translate?" : mode === "alithyagpt" ? "What are you working on?" : "Start a conversation"}</h1>
      {mode === null ? (
        <div className="mode-choices" aria-label="Conversation type">
          <button type="button" onClick={() => onSelect("translate")}><span className="choice-icon"><MessageSquareText /></span><strong>Translate</strong><small>Translation, revision, and terminology advice</small></button>
          <button type="button" onClick={() => onSelect("alithyagpt")}><span className="choice-icon"><Sparkles /></span><strong>AlithyaGPT</strong><small>Professional writing and communications advice</small></button>
        </div>
      ) : (
        <p>{mode === "translate" ? "Share your direction, audience, and requirements with the source text." : "Draft, revise, translate, or talk through a professional communication."}</p>
      )}
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<SessionUser | null | undefined>(undefined);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [owner, setOwner] = useState<Account>();
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [detail, setDetail] = useState<ThreadDetail>();
  const [draftMode, setDraftMode] = useState<Mode | null>(null);
  const [generation, setGeneration] = useState<GenerationSnapshot | null>(null);
  const [usage, setUsage] = useState("$0.00");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [handoff, setHandoff] = useState<ContentBlock[] | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [globalError, setGlobalError] = useState("");
  const preserveDraftOnOwnerChange = useRef(false);

  const bootstrap = useCallback(async () => {
    try {
      const current = await api.me();
      if (current.csrf_token) setCsrfToken(current.csrf_token);
      const [availableAccounts, total] = await Promise.all([api.accounts(), api.usage()]);
      setUser(current);
      setAccounts(availableAccounts);
      setOwner(availableAccounts.find((a) => a.id === current.id) ?? availableAccounts[0]);
      setUsage(total.formatted);
    } catch {
      setUser(null);
    }
  }, []);
  useEffect(() => { void bootstrap(); }, [bootstrap]);
  useEffect(() => {
    const unauthorized = () => setUser(null);
    window.addEventListener("oveo:unauthorized", unauthorized);
    return () => window.removeEventListener("oveo:unauthorized", unauthorized);
  }, []);

  const refreshThreads = useCallback(async () => {
    if (!owner) return;
    try { setThreads(await api.threads(owner.id)); } catch (reason) { setGlobalError(reason instanceof Error ? reason.message : "Could not load conversations."); }
  }, [owner]);
  useEffect(() => {
    setDetail(undefined);
    if (preserveDraftOnOwnerChange.current) preserveDraftOnOwnerChange.current = false;
    else setDraftMode(null);
    void refreshThreads();
  }, [owner, refreshThreads]);
  useEffect(() => {
    if (!owner) return;
    const timer = window.setInterval(() => void refreshThreads(), 5000);
    return () => window.clearInterval(timer);
  }, [owner, refreshThreads]);

  const openThread = useCallback(async (id: string, navigate = true) => {
    setDraftMode(null);
    setSidebarOpen(false);
    const loaded = await api.thread(id);
    setDetail(loaded);
    setGeneration(loaded.generation ?? null);
    if (navigate) updateAddress(conversationPath(id));
  }, []);

  useEffect(() => {
    if (!user || accounts.length === 0) return;
    const applyAddress = () => {
      const id = conversationIdFromPath(window.location.pathname);
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
      if (window.location.pathname === "/") updateAddress("/new", true);
    };
    applyAddress();
    window.addEventListener("popstate", applyAddress);
    return () => window.removeEventListener("popstate", applyAddress);
  }, [accounts.length, openThread, user]);

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
        void api.usage().then((result) => setUsage(result.formatted));
      }
    };
    // EventSource reconnects automatically after a transient network interruption.
    source.onerror = () => undefined;
    return () => source.close();
  }, [generationId, generationStatus, openThread, refreshThreads]);

  useEffect(() => {
    const context = (document as Document & { modelContext?: WebMcpContext }).modelContext;
    if (!context?.registerTool || !user || !owner) return;
    const lifecycle = new AbortController();
    void Promise.resolve(context.registerTool({
      name: "start_oveo_conversation",
      title: "Start an Oveo conversation",
      description: "Open a new unsaved Translate or AlithyaGPT conversation in the visible Oveo interface.",
      inputSchema: {
        type: "object",
        properties: {
          mode: { type: "string", enum: ["translate", "alithyagpt"] },
          ownerUsername: { type: "string", enum: accounts.map((account) => account.username) },
        },
        required: ["mode"],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      async execute(input) {
        const value = input as { mode?: unknown; ownerUsername?: unknown };
        if (value.mode !== "translate" && value.mode !== "alithyagpt") throw new Error("Invalid mode.");
        const selectedOwner = value.ownerUsername === undefined
          ? owner
          : accounts.find((account) => account.username === value.ownerUsername);
        if (!selectedOwner) throw new Error("That account is not available.");
        if (selectedOwner.id !== owner.id) preserveDraftOnOwnerChange.current = true;
        setOwner(selectedOwner);
        setDetail(undefined);
        setGeneration(null);
        setDraftMode(value.mode);
        setSidebarOpen(false);
        updateAddress("/new");
        return { state: "ready", mode: value.mode, ownerUsername: selectedOwner.username };
      },
    }, { signal: lifecycle.signal })).catch(() => undefined);
    return () => lifecycle.abort();
  }, [accounts, owner, user]);

  async function send(text: string, attachment?: File) {
    if (!owner) return;
    const result = await api.submit({
      threadId: detail?.id,
      ownerId: detail ? undefined : owner.id,
      mode: detail ? undefined : draftMode ?? undefined,
      voiceKey: !detail && draftMode === "alithyagpt" ? "comm_internes" : undefined,
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
  async function rename() {
    if (!detail) return;
    const title = window.prompt("Conversation title", detail.title)?.trim();
    if (!title) return;
    await api.rename(detail.id, title);
    setDetail({ ...detail, title });
    await refreshThreads();
  }
  async function removeThread() {
    if (!detail) return;
    await api.deleteThread(detail.id);
    setDeleteOpen(false);
    setDetail(undefined);
    setGeneration(null);
    updateAddress("/new", true);
    await refreshThreads();
  }
  async function createHandoff() {
    if (!detail) return;
    const { generation_id } = await api.handoff(detail.id, uuid());
    let current = await api.generation(generation_id);
    setHandoff([]);
    while (["queued", "running"].includes(current.status)) {
      await new Promise((resolve) => window.setTimeout(resolve, 700));
      current = await api.generation(generation_id);
      setHandoff(current.blocks);
    }
    if (current.status !== "completed") setGlobalError(current.error_message ?? "Prompt handoff could not be created.");
    void api.usage().then((result) => setUsage(result.formatted));
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
  function changeOwner(id: string) {
    setOwner(accounts.find((account) => account.id === id));
    updateAddress("/new");
  }

  if (user === undefined) return <div className="app-loading"><BrandLogo /></div>;
  if (!user) return <Login onLogin={() => void bootstrap()} />;
  const active = generation && generation.thread_id === detail?.id ? generation : null;

  return (
    <div className="app-shell">
      <button className="mobile-menu" onClick={() => setSidebarOpen(true)} aria-label="Open sidebar"><Menu /></button>
      <aside className={sidebarOpen ? "sidebar open" : "sidebar"}>
        <div className="sidebar-head"><BrandLogo compact /><button className="mobile-close icon-button" onClick={() => setSidebarOpen(false)} aria-label="Close sidebar"><X /></button></div>
        <button className="new-button" onClick={startNewConversation}><Plus /> New</button>
        {user.role === "owner" && (
          <label className="account-picker"><span>Viewing</span><select value={owner?.id} onChange={(e) => changeOwner(e.target.value)}>{accounts.map((account) => <option value={account.id} key={account.id}>{account.display_name}</option>)}</select></label>
        )}
        <nav className="thread-list" aria-label="Conversations">
          {threads.map((thread) => (
            <button className={detail?.id === thread.id ? "thread-item selected" : "thread-item"} onClick={() => void openThread(thread.id)} key={thread.id}>
              <div><span className="thread-title">{thread.title}</span>{thread.active_generation_id && <span className="activity-dot" aria-label="Generating" />}</div>
              <ModeBadge mode={thread.mode} />
            </button>
          ))}
        </nav>
        <footer className="sidebar-footer"><span className="cost">{usage}</span><button className="icon-button" onClick={logout} aria-label="Sign out"><LogOut /></button></footer>
      </aside>
      <main className="workspace">
        {detail && (
          <header className="thread-header">
            <div><h1>{detail.title}</h1><ModeBadge mode={detail.mode} /></div>
            <div className="header-actions">
              <button onClick={createHandoff}><Sparkles /> Create prompt handoff</button>
              <button className="icon-button" onClick={rename} aria-label="Rename conversation"><PenLine /></button>
              <button className="icon-button danger" onClick={() => setDeleteOpen(true)} aria-label="Delete conversation"><Trash2 /></button>
              <button className="icon-button compact-menu" aria-label="Conversation actions"><MoreHorizontal /></button>
            </div>
          </header>
        )}
        <section className="conversation-area">
          {detail ? <MessageList detail={detail} generation={active} /> : <EmptyThread mode={draftMode} onSelect={setDraftMode} />}
        </section>
        {(detail || draftMode) && <Composer activeGeneration={active} onSend={send} onStop={() => active ? api.stop(active.id) : Promise.resolve()} onRetry={retry} />}
      </main>
      {deleteOpen && detail && <Modal title="Delete conversation?" onClose={() => setDeleteOpen(false)}><p className="modal-copy">This permanently deletes the conversation and its attachments. This cannot be undone.</p><div className="modal-actions"><button onClick={() => setDeleteOpen(false)}>Cancel</button><button className="danger-button" onClick={removeThread}>Delete permanently</button></div></Modal>}
      {handoff !== null && <Modal title="Prompt handoff" onClose={() => setHandoff(null)}><div className="handoff-body">{handoff.length ? <ResponseBlocks blocks={handoff.map((block) => ({ ...block, type: "deliverable" }))} /> : <div className="handoff-loading">Creating maintenance brief…</div>}</div></Modal>}
      {globalError && <div className="toast" role="alert">{globalError}<button onClick={() => setGlobalError("")} aria-label="Dismiss"><X /></button></div>}
    </div>
  );
}
