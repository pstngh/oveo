import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Composer, conversationIdFromPath, conversationPath, EmptyThread, MessageList, ResponseBlocks, SidebarHeader } from "./App";
import type { ThreadDetail } from "./types";

afterEach(cleanup);

describe("deliverable copy", () => {
  it("copies only the exact plain deliverable text", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    render(<ResponseBlocks blocks={[
      { type: "deliverable", text: "First line\n\nSecond **literal** line" },
      { type: "advice", text: "Do not copy this note." },
    ]} />);
    const copyButton = screen.getByRole("button", { name: "Copy deliverable" });
    expect(copyButton).toHaveTextContent("");
    await user.click(copyButton);
    expect(writeText).toHaveBeenCalledWith("First line\n\nSecond **literal** line");
    expect(screen.getByRole("button", { name: "Copied" })).toHaveTextContent("");
  });
});

describe("composer attachments", () => {
  it("does not render keyboard shortcut hint text", () => {
    render(<Composer activeGeneration={null} onSend={vi.fn()} onStop={vi.fn()} onRetry={vi.fn()} />);

    expect(screen.queryByText("Enter to send · Shift+Enter for a new line")).not.toBeInTheDocument();
  });

  it("shows prompt handoff as a compact composer icon", async () => {
    const user = userEvent.setup();
    const onCreateHandoff = vi.fn();
    render(<Composer activeGeneration={null} onSend={vi.fn()} onStop={vi.fn()} onRetry={vi.fn()} onCreateHandoff={onCreateHandoff} />);

    const handoff = screen.getByRole("button", { name: "Create prompt handoff" });
    expect(handoff).not.toHaveTextContent("Create prompt handoff");
    await user.click(handoff);
    expect(onCreateHandoff).toHaveBeenCalledOnce();
  });

  it("turns a 4,000-character paste into a synthetic attachment and preserves typed instruction text", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(<Composer activeGeneration={null} onSend={onSend} onStop={vi.fn()} onRetry={vi.fn()} />);
    const textbox = screen.getByRole("textbox", { name: "Message" });
    await user.type(textbox, "Translate this carefully: ");
    textbox.focus();
    await user.paste("x".repeat(4000));
    expect(screen.getByText("Pasted text.txt")).toBeInTheDocument();
    expect(textbox).toHaveValue("Translate this carefully: ");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(onSend).toHaveBeenCalledWith(
      "Translate this carefully: ",
      expect.objectContaining({ name: "Pasted text.txt" }),
    );
  });

  it("rejects a second source attachment without replacing the first", async () => {
    const user = userEvent.setup();
    render(<Composer activeGeneration={null} onSend={vi.fn()} onStop={vi.fn()} onRetry={vi.fn()} />);
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["first"], "first.txt", { type: "text/plain" }));
    const textbox = screen.getByRole("textbox", { name: "Message" });
    textbox.focus();
    await user.paste("y".repeat(4000));
    expect(screen.getByRole("alert")).toHaveTextContent("Remove the current attachment");
    expect(screen.getByText("first.txt")).toBeInTheDocument();
  });
});

describe("new conversation mode selection", () => {
  it("shows mode choices inline without the old subheader or writing voices", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<EmptyThread mode={null} onSelect={onSelect} />);

    expect(screen.getByRole("heading", { name: "Start a conversation" })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "Oveo" })).not.toBeInTheDocument();
    expect(screen.queryByText("Choose Translate or AlithyaGPT to begin.")).not.toBeInTheDocument();
    expect(screen.queryByText("Writing voices")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Translate/ }));
    expect(onSelect).toHaveBeenCalledWith("translate");
  });

  it("replaces the choices with mode-specific guidance after selection", () => {
    render(<EmptyThread mode="alithyagpt" onSelect={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "What are you working on?" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Translate/ })).not.toBeInTheDocument();
    expect(screen.getByText("Draft, revise, translate, or talk through a professional communication.")).toBeInTheDocument();
  });
});

describe("sidebar header", () => {
  it("keeps the mobile close control without Oveo branding", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    render(<SidebarHeader onClose={onClose} />);

    expect(screen.queryByRole("img", { name: "Oveo" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Close sidebar" }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});

describe("conversation scrolling", () => {
  it("scrolls the message history itself instead of moving the application viewport", () => {
    const scrollHeight = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(900);
    const detail = {
      id: "thread-1",
      owner_id: "user-1",
      owner_username: "charles",
      mode: "translate",
      voice_key: null,
      title: "Long conversation",
      updated_at: "2026-09-16T00:00:00Z",
      active_generation_id: null,
      messages: [],
    } satisfies ThreadDetail;

    const { container } = render(<MessageList detail={detail} generation={null} />);
    expect((container.firstElementChild as HTMLDivElement).scrollTop).toBe(900);

    scrollHeight.mockRestore();
  });

  it("does not render speaker labels", () => {
    const detail = {
      id: "thread-1",
      owner_id: "user-1",
      owner_username: "charles",
      mode: "translate",
      voice_key: null,
      title: "Compact conversation",
      updated_at: "2026-09-16T00:00:00Z",
      active_generation_id: null,
      messages: [
        { id: "message-1", role: "user", actor_username: "charles", blocks: [{ type: "conversation", text: "Hello" }], attachment: null, created_at: "2026-09-16T00:00:00Z" },
        { id: "message-2", role: "assistant", actor_username: null, blocks: [{ type: "conversation", text: "Hi" }], attachment: null, created_at: "2026-09-16T00:00:01Z" },
      ],
    } satisfies ThreadDetail;

    render(<MessageList detail={detail} generation={null} />);
    expect(screen.queryByText(/^You$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Oveo$/)).not.toBeInTheDocument();
  });
});

describe("conversation addresses", () => {
  it("builds and reads a visible conversation path", () => {
    expect(conversationPath("thread-1")).toBe("/conversations/thread-1");
    expect(conversationIdFromPath("/conversations/thread-1")).toBe("thread-1");
    expect(conversationIdFromPath("/new")).toBeNull();
  });
});
