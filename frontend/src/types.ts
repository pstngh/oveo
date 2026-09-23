export type Mode = "translate" | "revision" | "internal_comms";
export type BlockType = "conversation" | "deliverable" | "advice";
export type AttachmentRole = "source" | "reference";

export interface ContentBlock {
  type: BlockType;
  text: string;
}

export interface Account {
  id: string;
  username: "charles" | "yousra";
  display_name: string;
}

export interface SessionUser extends Account {
  csrf_token?: string;
}

export interface ThreadSummary {
  id: string;
  owner_id: string;
  mode: Mode;
  title: string;
  updated_at: string;
  active_generation_id: string | null;
}

export interface AttachmentInfo {
  filename: string;
  role: AttachmentRole;
  byte_size: number;
  word_count: number;
  media_type: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  actor_username: string | null;
  blocks: ContentBlock[];
  attachment: AttachmentInfo | null;
  created_at: string;
}

export interface ThreadDetail extends ThreadSummary {
  owner_username: string;
  messages: Message[];
  docx_exportable?: boolean;
  /** The latest chat generation unless it completed. */
  generation?: GenerationSnapshot | null;
  /** A prompt handoff that is still running, reported apart from the chat turn. */
  handoff?: GenerationSnapshot | null;
}

export interface GenerationSnapshot {
  id: string;
  thread_id: string | null;
  status: "queued" | "running" | "stopping" | "completed" | "failed" | "stopped";
  blocks: ContentBlock[];
  error_code?: string | null;
  error_message?: string | null;
  retryable?: boolean;
  seq: number;
}
