export type Mode = "translate" | "revision" | "internal_comms";
export type BlockType = "conversation" | "deliverable" | "advice";

export interface ContentBlock {
  type: BlockType;
  text: string;
}

export interface Account {
  id: string;
  username: "charles" | "yousra";
  display_name: string;
  role: "owner" | "user";
}

export interface SessionUser extends Account {
  csrf_token?: string;
}

export interface ThreadSummary {
  id: string;
  owner_id: string;
  mode: Mode;
  voice_key: string | null;
  title: string;
  updated_at: string;
  active_generation_id: string | null;
}

export interface AttachmentInfo {
  filename: string;
  byte_size: number;
  word_count: number;
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
  generation?: GenerationSnapshot | null;
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
