import { describe, expect, it } from "vitest";

import { compactionResultText } from "./SessionTreePanel";

const translate = (zh: string, _en: string) => zh;

describe("Session tree compaction", () => {
  it("reports an already compact context without claiming token savings", () => {
    expect(compactionResultText({
      status: "not_needed",
      original_tokens: 141,
      compacted_tokens: 141,
      saved_tokens: 0,
    }, translate)).toBe("无需压缩：当前上下文已经足够精简（141 tokens）");
  });

  it("reports the measured reduction after a useful compaction", () => {
    expect(compactionResultText({
      original_tokens: 1200,
      compacted_tokens: 500,
      saved_tokens: 700,
    }, translate)).toContain("1200 → 500 tokens，节省 700");
  });
});
