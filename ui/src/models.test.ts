import { describe, expect, it } from "vitest";
import { ReferenceSchema, displayText, locationLabel, mergeRun, protocolMessages, type Run } from "./models";

describe("reference presentation", () => {
  it("keeps normal answer text and hides machine metadata, including unfinished streaming blocks", () => {
    expect(displayText("Answer\n```workiq_references\n[{\"title\":\"Plan\"}]\n```\nMore"))
      .toBe("Answer\n\nMore");
    expect(displayText("Answer\n```workiq_references\n[{")).toBe("Answer");
    expect(displayText("```python\nprint(1)\n```")).toContain("print(1)");
  });

  it("labels reported slide/page positions without accepting verification claims or unsafe links", () => {
    const reference = ReferenceSchema.parse({ title: "Plan", url: "https://contoso.sharepoint.com/plan", slide: 4 });
    expect(locationLabel(reference)).toBe("Reported slide 4");
    expect(reference.location_verified).toBe(false);
    expect(ReferenceSchema.safeParse({ ...reference, location_verified: true }).success).toBe(false);
    expect(ReferenceSchema.safeParse({ ...reference, url: "javascript:alert(1)" }).success).toBe(false);
  });
});

describe("per-prompt activity", () => {
  it("updates a streaming run in place instead of duplicating its calls or replacing another prompt", () => {
    const first: Run = {
      id: "11111111-1111-4111-8111-111111111111",
      session_id: "22222222-2222-4222-8222-222222222222",
      started_at: "2026-10-14T10:00:00Z", prompt_preview: "First", status: "running",
      response_id: null, assistant_message_ids: [], activity: [],
    };
    const second: Run = { ...first, id: "33333333-3333-4333-8333-333333333333", prompt_preview: "Second" };
    const updated = { ...first, status: "completed" as const, assistant_message_ids: ["msg_1"] };
    const runs = mergeRun([first, second], updated);
    expect(runs).toHaveLength(2);
    expect(runs.find((run) => run.id === first.id)?.status).toBe("completed");
    expect(runs.find((run) => run.id === second.id)?.prompt_preview).toBe("Second");
  });

  it("does not send preview metadata back as user-controlled agent tools", () => {
    const messages = protocolMessages([{
      id: "msg_1", role: "assistant", content: "A source",
      references: [ReferenceSchema.parse({ title: "Plan", url: "https://contoso.sharepoint.com/plan" })],
    }]);
    expect(messages).toEqual([{ id: "msg_1", role: "assistant", content: "A source" }]);
  });
});
