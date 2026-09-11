import { afterEach, describe, expect, it, vi } from "vitest";

import { streamEvents } from "./api";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

describe("runtime event stream", () => {
  it("reports a successful reconnect as soon as the SSE response opens", async () => {
    const opened = vi.fn();
    globalThis.fetch = vi.fn(async () => new Response(
      new ReadableStream({
        start(controller) {
          controller.close();
        },
      }),
      { status: 200 },
    ));

    await streamEvents(
      "thread-1",
      0,
      new AbortController().signal,
      () => undefined,
      undefined,
      opened,
    );

    expect(opened).toHaveBeenCalledOnce();
  });
});
