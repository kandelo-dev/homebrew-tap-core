import { describe, expect, it } from "vitest";

import { waitForFramebufferEvidence } from "../framebuffer-evidence-readiness.ts";

describe("Formula framebuffer evidence readiness", () => {
  it("waits past black startup writes for the required rendered pixels", async () => {
    const snapshots = [
      { binds: 1, writes: 1, nonBlankPixels: 0, exited: false },
      { binds: 1, writes: 31, nonBlankPixels: 0, exited: false },
      { binds: 1, writes: 32, nonBlankPixels: 1_500, exited: false },
    ];
    let now = 0;
    let sampled = 0;

    const result = await waitForFramebufferEvidence({
      minWrites: 1,
      minNonBlankPixels: 1_000,
      deadline: 30_000,
      now: () => now,
      delay: async (milliseconds) => {
        now += milliseconds;
      },
      sample: () => snapshots[Math.min(sampled++, snapshots.length - 1)]!,
    });

    expect(sampled).toBe(3);
    expect(result).toEqual(snapshots[2]);
  });
});
