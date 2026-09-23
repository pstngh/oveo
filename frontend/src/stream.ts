import { api, ApiError } from "./api";
import type { ContentBlock, GenerationSnapshot } from "./types";

export type DeltaOp =
  | { op: "start"; type: ContentBlock["type"] }
  | { op: "append"; text: string }
  | { op: "status"; status: GenerationSnapshot["status"] };

export interface GenerationDelta {
  from: number;
  seq: number;
  ops: DeltaOp[];
}

const TERMINAL = new Set<GenerationSnapshot["status"]>(["completed", "failed", "stopped"]);
const POLL_MS = 2000;
const RENDER_MS = 50;
const MAX_RESYNCS = 3;

export function isTerminal(status: GenerationSnapshot["status"]) {
  return TERMINAL.has(status);
}

/** Apply one delta to a snapshot, or return null on a sequence gap (resynchronize). */
export function applyDelta(snapshot: GenerationSnapshot, delta: GenerationDelta): GenerationSnapshot | null {
  if (delta.from !== snapshot.seq + 1) return null;
  const blocks = snapshot.blocks.slice();
  let status = snapshot.status;
  for (const op of delta.ops) {
    if (op.op === "start") {
      blocks.push({ type: op.type, text: "" });
    } else if (op.op === "append") {
      const last = blocks[blocks.length - 1];
      if (!last) return null;
      blocks[blocks.length - 1] = { ...last, text: last.text + op.text };
    } else {
      status = op.status;
    }
  }
  return { ...snapshot, blocks, status, seq: delta.seq };
}

/**
 * Follow one generation: a snapshot on connect, then small ordered deltas. A gap or a
 * reconnect starts over from a fresh snapshot; a stream the browser gave up on (for
 * example after 401 or 404) falls back to polling. Returns a function that stops it.
 */
export function followGeneration(
  id: string,
  handlers: { onUpdate: (snapshot: GenerationSnapshot) => void; onUnavailable?: () => void },
): () => void {
  let current: GenerationSnapshot | null = null;
  let source: EventSource | null = null;
  let stopped = false;
  let resyncs = 0;
  let pollTimer: number | undefined;
  let renderTimer: number | undefined;

  const publish = (immediate: boolean) => {
    if (stopped || !current) return;
    if (immediate) {
      window.clearTimeout(renderTimer);
      renderTimer = undefined;
      handlers.onUpdate(current);
      return;
    }
    // Coalesce rapid deltas into at most one render per interval.
    renderTimer ??= window.setTimeout(() => {
      renderTimer = undefined;
      if (!stopped && current) handlers.onUpdate(current);
    }, RENDER_MS);
  };

  const close = () => {
    source?.close();
    source = null;
  };

  const poll = async () => {
    if (stopped) return;
    try {
      current = await api.generation(id);
      publish(true);
      if (isTerminal(current.status)) return;
    } catch (reason) {
      if (reason instanceof ApiError && (reason.status === 401 || reason.status === 404)) {
        handlers.onUnavailable?.();
        return;
      }
    }
    pollTimer = window.setTimeout(() => void poll(), POLL_MS);
  };

  const open = () => {
    if (stopped) return;
    const stream = new EventSource(`/api/generations/${encodeURIComponent(id)}/events`);
    source = stream;
    stream.addEventListener("snapshot", (event) => {
      current = JSON.parse((event as MessageEvent<string>).data) as GenerationSnapshot;
      resyncs = 0;
      publish(true);
      if (isTerminal(current.status)) close();
    });
    stream.addEventListener("delta", (event) => {
      if (!current) return;
      const next = applyDelta(current, JSON.parse((event as MessageEvent<string>).data) as GenerationDelta);
      if (!next) {
        // A missed delta: reconnect, which starts over from a fresh snapshot.
        close();
        if (resyncs++ < MAX_RESYNCS) open();
        else void poll();
        return;
      }
      // Status changes render at once; text deltas are coalesced.
      const statusChanged = next.status !== current.status;
      current = next;
      publish(statusChanged);
    });
    stream.onerror = () => {
      // Transient errors reconnect automatically (and resend a snapshot). A closed
      // stream will not come back, so keep the view current by polling instead.
      if (stream.readyState === EventSource.CLOSED) {
        close();
        void poll();
      }
    };
  };

  open();
  return () => {
    stopped = true;
    close();
    window.clearTimeout(pollTimer);
    window.clearTimeout(renderTimer);
  };
}
