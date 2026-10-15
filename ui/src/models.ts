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
  kind: z.enum(["tool", "skill"]),
  skill: z.string().nullable(),
  status: z.enum(["running", "completed", "failed", "cancelled"]),
  started_at: z.string(),
  duration_ms: z.number().nonnegative().nullable(),
  timing_source: z.literal("runtime").nullable().default(null),
});

export const RunSchema = z.object({
  id: z.string().uuid(),
  session_id: z.string().uuid(),
  prompt_preview: z.string(),
  status: z.enum(["running", "completed", "incomplete", "failed", "cancelled"]),
  started_at: z.string(),
  response_id: z.string().nullable(),
  assistant_message_ids: z.array(z.string()),
  activity: z.array(ActivitySchema),
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
  if (call.duration_ms === 0) return "<1 ms";
  if (call.duration_ms < 1000) return `${call.duration_ms} ms`;
  return `${(call.duration_ms / 1000).toFixed(1)}s`;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed. Please retry.";
}
