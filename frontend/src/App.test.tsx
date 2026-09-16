import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Composer, ResponseBlocks } from "./App";

afterEach(cleanup);

describe("deliverable copy", () => {
  it("copies only the exact plain deliverable text", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    render(<ResponseBlocks blocks={[
      { type: "deliverable", text: "First line\n\nSecond **literal** line" },
      { type: "advice", text: "Do not copy this note." },
    ]} />);
    await user.click(screen.getByRole("button", { name: "Copy deliverable" }));
    expect(writeText).toHaveBeenCalledWith("First line\n\nSecond **literal** line");
  });
});

describe("composer attachments", () => {
  it("turns a 4,000-character paste into a synthetic attachment and preserves typed instruction text", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn().mockResolvedValue(undefined);
    render(<Composer activeGeneration={null} onSend={onSend} onStop={vi.fn()} onRetry={vi.fn()} />);
    const textbox = screen.getByRole("textbox", { name: "Message Oveo" });
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
    const textbox = screen.getByRole("textbox", { name: "Message Oveo" });
    textbox.focus();
    await user.paste("y".repeat(4000));
    expect(screen.getByRole("alert")).toHaveTextContent("Remove the current attachment");
    expect(screen.getByText("first.txt")).toBeInTheDocument();
  });
});
