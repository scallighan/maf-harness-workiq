import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Activity, MarkdownAnswer } from "./App";
import type { Run } from "./models";

describe("assistant Markdown", () => {
  it("renders GFM tables, headings, bold text, lists and code as actual elements", () => {
    const content = [
      "## Holdings", "", "**Portfolio**", "",
      "| Ticker | Security | Quantity |", "| --- | --- | ---: |", "| ABC | Example Corp | 10 |", "",
      "- A source", "", "```text", "read-only", "```",
    ].join("\n");
    const html = renderToStaticMarkup(<MarkdownAnswer content={content} />);
    expect(html).toContain("<h2>Holdings</h2>");
    expect(html).toContain("<strong>Portfolio</strong>");
    expect(html).toContain("<table>");
    expect(html).toContain("<th>Ticker</th>");
    expect(html).toContain("<td>Example Corp</td>");
    expect(html).toContain('class="markdown-table"');
    expect(html).toContain("<li>A source</li>");
    expect(html).toContain("<pre><code");
  });

  it("does not execute raw HTML, fetch external images or expose machine source metadata", () => {
    const content = [
      '<script>alert("unsafe")</script>',
      "![source](https://evil.example.com/image.png)",
      "```workiq_references", '[{"title":"Hidden metadata"}]', "```",
    ].join("\n\n");
    const html = renderToStaticMarkup(<MarkdownAnswer content={content} />);
    expect(html).not.toContain("<script");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("Hidden metadata");
    expect(html).toContain("Image omitted");
  });
});

describe("run progress", () => {
  it("shows real source progress separately from model tool calls and displays measured response latency", () => {
    const run: Run = {
      id: "11111111-1111-4111-8111-111111111111",
      session_id: "22222222-2222-4222-8222-222222222222",
      started_at: "2026-10-15T10:00:00Z", prompt_preview: "Find sources", status: "running",
      response_id: "resp1", assistant_message_ids: [], stage: "searching",
      latency: { preparation_ms: 10, first_event_ms: 20, first_text_ms: 3500, response_ms: null },
      activity: [{
        call_id: "source1", name: "workiq_source_search___Email", kind: "search", source: "Email",
        parent_call_id: "parent", skill: null, status: "completed", started_at: "2026-10-15T10:00:01Z",
        duration_ms: 2400, timing_source: "runtime",
      }],
    };
    const html = renderToStaticMarkup(<Activity run={run} loadDetails={async () => ({
      input: null, output: null, input_truncated: false, output_truncated: false,
    })} />);
    expect(html).toContain("Source search: Email");
    expect(html).toContain("Searching your work sources");
    expect(html).toContain("0 skill calls / 0 tool calls");
    expect(html).toContain("First text: 3.5s");
    expect(html).not.toContain("Input / output");
    expect(html).not.toContain("Response ready");
  });

  it("shows an obvious active state before the first tool call without inventing tool activity", () => {
    const run: Run = {
      id: "11111111-1111-4111-8111-111111111111",
      session_id: "22222222-2222-4222-8222-222222222222",
      started_at: "2026-10-14T10:00:00Z", prompt_preview: "Hello", status: "running",
      response_id: null, assistant_message_ids: [], activity: [],
    };
    const html = renderToStaticMarkup(<Activity run={run} loadDetails={async () => ({
      input: null, output: null, input_truncated: false, output_truncated: false,
    })} />);
    expect(html).toContain('class="activity is-running"');
    expect(html).toContain('class="spinner"');
    expect(html).toContain("Request in progress");
    expect(html).toContain("Waiting for the hosted agent");
    expect(html).not.toContain("No tool or skill calls were reported");
  });

  it("provides an expandable input/output control for an actual completed call", () => {
    const run: Run = {
      id: "11111111-1111-4111-8111-111111111111",
      session_id: "22222222-2222-4222-8222-222222222222",
      started_at: "2026-10-14T10:00:00Z", prompt_preview: "Hello", status: "completed",
      response_id: "resp1", assistant_message_ids: [], activity: [{
        call_id: "call1", name: "workiq___ask", status: "completed", kind: "tool",
        skill: null, started_at: "2026-10-14T10:00:00Z", duration_ms: 0, timing_source: null,
      }],
    };
    const html = renderToStaticMarkup(<Activity run={run} loadDetails={async () => ({
      input: "{}", output: "Hello", input_truncated: false, output_truncated: false,
    })} />);
    expect(html).toContain('class="tool-details"');
    expect(html).toContain("Input / output");
    expect(html).toContain("workiq / ask");
    expect(html).toContain("0 skill calls / 1 tool call");
    expect(html).not.toContain("Skill loaded");
    expect(html).toContain("Timing unavailable");
    expect(html).not.toContain("0.0s");
  });

  it.each(["completed", "failed"] as const)("shows an actual %s skill load without claiming rule verification", (status) => {
    const run: Run = {
      id: "11111111-1111-4111-8111-111111111111",
      session_id: "22222222-2222-4222-8222-222222222222",
      started_at: "2026-10-14T10:00:00Z", prompt_preview: "Find a presentation", status: "completed",
      response_id: "resp1", assistant_message_ids: [], activity: [{
        call_id: "skill1", name: "load_skill", status, kind: "skill",
        skill: "document-finder", started_at: "2026-10-14T10:00:00Z", duration_ms: 2, timing_source: "runtime",
      }],
    };
    const html = renderToStaticMarkup(<Activity run={run} loadDetails={async () => ({
      input: '{"skill_name":"document-finder"}', output: "Skill instructions",
      input_truncated: false, output_truncated: false,
    })} />);
    expect(html).toContain("1 skill call / 0 tool calls");
    expect(html).toContain("Skill: document-finder");
    expect(html).toContain("not that every rule was followed");
    expect(html.includes("Skill loaded")).toBe(status === "completed");
    if (status === "failed") expect(html).toContain("Error");
    expect(html).toContain("Input / output");
    expect(html).toContain("2 ms");
  });
});
