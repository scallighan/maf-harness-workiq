import type { Message } from "@ag-ui/core";
import { z } from "zod";

const httpsUrl = z.string().url().refine((value) => {
  const url = new URL(value);
  return url.protocol === "https:" && !url.username && !url.password;
}, "Expected a secure URL without credentials");

export const ConfigSchema = z.object({
  tenantId: z.string().uuid(),
  clientId: z.string().uuid(),
  scope: z.string().startsWith("api://"),
  agentName: z.string(),
});

export const ReferenceSchema = z.object({
  title: z.string(),
  url: httpsUrl,
  drive_id: z.string().nullable().default(null),
  item_id: z.string().nullable().default(null),
  page: z.number().int().positive().nullable().default(null),
  slide: z.number().int().positive().nullable().default(null),
  location_verified: z.literal(false).default(false),
});

export const SessionSchema = z.object({
  id: z.string().uuid(),
  title: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  running: z.boolean(),
});

export const ActivitySchema = z.object({
  call_id: z.string(),
  name: z.string(),
  kind: z.enum(["tool", "skill", "search"]),
  skill: z.string().nullable(),
  source: z.enum(["Email", "OneDriveAndSharePoint"]).nullable().optional(),
  parent_call_id: z.string().nullable().optional(),
  status: z.enum(["running", "completed", "failed", "cancelled"]),
  started_at: z.string(),
  duration_ms: z.number().nonnegative().nullable(),
  timing_source: z.literal("runtime").nullable().default(null),
}).transform((call) => call.source ? { ...call, kind: "search" as const } : call);

export const RunSchema = z.object({
  id: z.string().uuid(),
  session_id: z.string().uuid(),
  prompt_preview: z.string(),
  status: z.enum(["running", "completed", "incomplete", "failed", "cancelled"]),
  started_at: z.string(),
  response_id: z.string().nullable(),
  assistant_message_ids: z.array(z.string()),
  activity: z.array(ActivitySchema),
  stage: z.enum(["connecting", "planning", "loading_skill", "searching", "running_tool", "answering"]).optional(),
  latency: z.object({
    preparation_ms: z.number().nonnegative().nullable(),
    first_event_ms: z.number().nonnegative().nullable(),
    first_text_ms: z.number().nonnegative().nullable(),
    response_ms: z.number().nonnegative().nullable(),
  }).optional(),
});

export const ToolPayloadSchema = z.object({
  input: z.string().nullable(),
  output: z.string().nullable(),
  input_truncated: z.boolean(),
  output_truncated: z.boolean(),
});

export const ChatMessageSchema = z.object({
  id: z.string(),
  role: z.enum(["user", "assistant"]),
  content: z.string(),
  references: z.array(ReferenceSchema).default([]),
});

export const ConversationSchema = z.object({
  session: SessionSchema,
  messages: z.array(ChatMessageSchema),
  runs: z.array(RunSchema),
});

export const PreviewSchema = z.object({
  get_url: httpsUrl.optional(),
  post_url: httpsUrl.optional(),
  post_parameters: z.string().optional(),
  warning: z.string().nullable(),
  position_verified: z.literal(false),
});

export const ConsentSchema = z.object({
  url: httpsUrl,
  response_id: z.string().nullable(),
});

export const ReferencesEventSchema = z.object({
  message_id: z.string(),
  references: z.array(ReferenceSchema),
});

export type PublicConfig = z.infer<typeof ConfigSchema>;
export type FileReference = z.infer<typeof ReferenceSchema>;
export type Session = z.infer<typeof SessionSchema>;
export type Run = z.infer<typeof RunSchema>;
export type ToolPayload = z.infer<typeof ToolPayloadSchema>;
export type ToolActivity = z.infer<typeof ActivitySchema>;
export type ChatMessage = z.infer<typeof ChatMessageSchema>;
export type Conversation = z.infer<typeof ConversationSchema>;
export type Preview = z.infer<typeof PreviewSchema>;
export type Consent = z.infer<typeof ConsentSchema>;

export function protocolMessages(messages: ChatMessage[]): Message[] {
  return messages.map((message) => message.role === "user"
    ? { id: message.id, role: "user", content: message.content }
    : { id: message.id, role: "assistant", content: message.content });
}

export function mergeStreamMessages(previous: ChatMessage[], incoming: readonly Readonly<Message>[]): ChatMessage[] {
  const byId = new Map(previous.map((message) => [message.id, message]));
  const next = incoming.flatMap((message) => {
    if ((message.role !== "user" && message.role !== "assistant") || typeof message.content !== "string") {
      return [];
    }
    const prior = byId.get(message.id);
    if (prior && prior.role === message.role && prior.content === message.content) return [prior];
    return [ChatMessageSchema.parse({ ...message, references: prior?.references ?? [] })];
  });
  return next.length === previous.length && next.every((message, index) => message === previous[index])
    ? previous : next;
}

export class MessageBatcher {
  private pending: readonly Readonly<Message>[] | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;

  constructor(
    private readonly apply: (messages: readonly Readonly<Message>[]) => void,
    private readonly onError: (error: unknown) => void,
  ) {}

  push(messages: readonly Readonly<Message>[]) {
    this.pending = messages;
    if (this.timer !== undefined) return;
    this.timer = setTimeout(() => {
      try { this.flush(); }
      catch (error) { this.onError(error); }
    }, 50);
  }

  flush() {
    clearTimeout(this.timer);
    this.timer = undefined;
    const pending = this.pending;
    this.pending = null;
    if (pending) this.apply(pending);
  }

  cancel() {
    clearTimeout(this.timer);
    this.timer = undefined;
    this.pending = null;
  }
}

export function mergeRun(runs: Run[], incoming: Run): Run[] {
  return [...runs.filter((run) => run.id !== incoming.id), incoming]
    .sort((left, right) => left.started_at.localeCompare(right.started_at));
}

export function displayText(content: string): string {
  return content.replace(/```workiq_references\b[\s\S]*?(?:```|$)/g, "").trim();
}

export function locationLabel(reference: FileReference): string {
  if (reference.slide !== null) return `Reported slide ${reference.slide}`;
  if (reference.page !== null) return `Reported page ${reference.page}`;
  return "File reference";
}

export function toolDuration(call: ToolActivity): string {
  if (call.timing_source !== "runtime" || call.duration_ms === null) {
    return call.status === "running" ? "Timing pending" : "Timing unavailable";
  }
  return durationLabel(call.duration_ms);
}

export function durationLabel(milliseconds: number): string {
  if (milliseconds === 0) return "<1 ms";
  if (milliseconds < 1000) return `${milliseconds} ms`;
  return `${(milliseconds / 1000).toFixed(1)}s`;
}

export function runProgressLabel(run: Run): string {
  const stages = {
    connecting: "Connecting to the hosted agent",
    planning: "Preparing the response",
    loading_skill: "Loading research instructions",
    searching: "Searching your work sources",
    running_tool: "Running a tool",
    answering: "Streaming the response",
  };
  return run.stage ? stages[run.stage] : "Request in progress";
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed. Please retry.";
}
