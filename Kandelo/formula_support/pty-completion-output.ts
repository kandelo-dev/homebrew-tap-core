import { Buffer } from "node:buffer";

export interface PtyCompletionOutputTracker {
  observe(data: Uint8Array): void;
  wait(): Promise<void>;
}

export function validatePtyCompletionOutput(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.includes("\0") ||
    Buffer.byteLength(value, "utf8") > 4096
  ) {
    throw new Error(
      "completionOutput must be a nonempty string of at most 4096 bytes without NUL",
    );
  }
  return value;
}

export function createPtyCompletionOutputTracker(
  expected: string,
): PtyCompletionOutputTracker {
  const expectedBytes = Buffer.from(expected, "utf8");
  let tail = Buffer.alloc(0);
  let matched = false;
  let resolveMatch: (() => void) | undefined;
  const match = new Promise<void>((resolve) => {
    resolveMatch = resolve;
  });

  return {
    observe(data: Uint8Array): void {
      if (matched) return;
      const combined = Buffer.concat([tail, Buffer.from(data)]);
      if (combined.indexOf(expectedBytes) >= 0) {
        matched = true;
        resolveMatch?.();
        return;
      }
      const retainedBytes = Math.max(0, expectedBytes.byteLength - 1);
      tail = combined.subarray(Math.max(0, combined.byteLength - retainedBytes));
    },

    wait(): Promise<void> {
      return match;
    },
  };
}

export async function waitForPtyCompletion(
  exit: Promise<number>,
  timeout: Promise<number>,
  tracker?: PtyCompletionOutputTracker,
): Promise<number> {
  if (!tracker) return Promise.race([exit, timeout]);

  const result = await Promise.race([
    tracker.wait().then(() => ({ kind: "output" as const })),
    exit.then((status) => ({ kind: "exit" as const, status })),
    timeout,
  ]);
  if (typeof result === "number") return result;
  if (result.kind === "exit") {
    throw new Error(
      `process exited with status ${result.status} before required completion output`,
    );
  }
  return 0;
}
