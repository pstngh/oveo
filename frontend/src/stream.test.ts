import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import { applyDelta, followGeneration } from "./stream";
import type { GenerationSnapshot } from "./types";

const base: GenerationSnapshot = {
  id: "g1", thread_id: "t1", status: "running", blocks: [], error_code: null, error_message: null, retryable: false, seq: 4,
};

class FakeEventSource {
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = 1;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (event: MessageEvent<string>) => void>();
  constructor(public url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.set(type, listener);
  }
  emit(type: string, data: unknown) {
    this.listeners.get(type)?.({ data: JSON.stringify(data) } as MessageEvent<string>);
  }
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
});

describe("applyDelta", () => {
  it("appends new text in order and rejects a sequence gap", () => {
    const started = applyDelta(base, { from: 5, seq: 5, ops: [{ op: "start", type: "deliverable" }] });
    expect(started?.blocks).toEqual([{ type: "deliverable", text: "" }]);
    const appended = applyDelta(started!, {
      from: 6,
      seq: 7,
      ops: [{ op: "append", text: "Bon" }, { op: "append", text: "jour" }, { op: "status", status: "stopping" }],
    });
    expect(appended).toMatchObject({ seq: 7, status: "stopping", blocks: [{ type: "deliverable", text: "Bonjour" }] });
    expect(applyDelta(appended!, { from: 9, seq: 9, ops: [] })).toBeNull();
  });
});

describe("followGeneration", () => {
  it("renders the snapshot, coalesces deltas, and polls after a delta that does not follow", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", FakeEventSource);
    const poll = vi.spyOn(api, "generation").mockResolvedValue({
      ...base, status: "completed", seq: 12, blocks: [{ type: "deliverable", text: "ABCD" }],
    });
    const updates: GenerationSnapshot[] = [];
    const stop = followGeneration("g/1", { onUpdate: (snapshot) => updates.push(snapshot) });
    const first = FakeEventSource.instances[0];
    expect(first.url).toBe("/api/generations/g%2F1/events");

    first.emit("snapshot", { ...base, blocks: [{ type: "deliverable", text: "A" }] });
    first.emit("delta", { from: 5, seq: 5, ops: [{ op: "append", text: "B" }] });
    first.emit("delta", { from: 6, seq: 6, ops: [{ op: "append", text: "C" }] });
    expect(updates).toHaveLength(1); // deltas wait for the next render tick
    vi.advanceTimersByTime(60);
    expect(updates.at(-1)?.blocks[0].text).toBe("ABC");

    first.emit("delta", { from: 9, seq: 9, ops: [{ op: "append", text: "lost" }] });
    expect(first.readyState).toBe(FakeEventSource.CLOSED);
    await vi.waitFor(() => expect(updates.at(-1)).toMatchObject({ status: "completed", blocks: [{ text: "ABCD" }] }));
    expect(poll).toHaveBeenCalledOnce();
    expect(FakeEventSource.instances).toHaveLength(1);
    stop();
  });

  it("falls back to polling when the browser gives up on the stream", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const poll = vi
      .spyOn(api, "generation")
      .mockResolvedValue({ ...base, status: "failed", error_message: "The model provider could not be reached." });
    const updates: GenerationSnapshot[] = [];
    const stop = followGeneration("g1", { onUpdate: (snapshot) => updates.push(snapshot) });
    const stream = FakeEventSource.instances[0];
    stream.readyState = FakeEventSource.CLOSED; // e.g. 401 or 404 on reconnect
    stream.onerror?.();
    await vi.waitFor(() => expect(updates.at(-1)?.status).toBe("failed"));
    expect(poll).toHaveBeenCalledOnce();
    stop();
  });

  it("reports an unavailable generation instead of polling forever", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    vi.spyOn(api, "generation").mockRejectedValue(new ApiError(401, "authentication_required", "Sign in to continue."));
    const unavailable = vi.fn();
    const stop = followGeneration("g1", { onUpdate: vi.fn(), onUnavailable: unavailable });
    const stream = FakeEventSource.instances[0];
    stream.readyState = FakeEventSource.CLOSED;
    stream.onerror?.();
    await vi.waitFor(() => expect(unavailable).toHaveBeenCalledOnce());
    stop();
  });

  it("stays silent once stopped, even if a poll answers afterwards", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    let reject: (reason: unknown) => void = () => undefined;
    vi.spyOn(api, "generation").mockReturnValue(new Promise((_resolve, fail) => { reject = fail; }));
    const unavailable = vi.fn();
    const updates = vi.fn();
    const stop = followGeneration("g1", { onUpdate: updates, onUnavailable: unavailable });
    const stream = FakeEventSource.instances[0];
    stream.readyState = FakeEventSource.CLOSED;
    stream.onerror?.();

    stop();
    reject(new ApiError(404, "generation_not_found", "Generation not found."));
    await Promise.resolve();
    await Promise.resolve();

    expect(unavailable).not.toHaveBeenCalled();
    expect(updates).not.toHaveBeenCalled();
  });
});
