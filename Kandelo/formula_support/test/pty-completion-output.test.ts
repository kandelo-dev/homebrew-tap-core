import assert from "node:assert/strict";
import test from "node:test";

import {
  createPtyCompletionOutputTracker,
  validatePtyCompletionOutput,
  waitForPtyCompletion,
} from "../pty-completion-output.ts";

const never = new Promise<number>(() => {});
const bytes = (value: string) => new TextEncoder().encode(value);

test("completes only after observing the required literal across chunks", async () => {
  const tracker = createPtyCompletionOutputTracker(" gametics in ");
  const waiting = waitForPtyCompletion(never, never, tracker);

  tracker.observe(bytes("timed 123 game"));
  tracker.observe(bytes("tics in 45 realtics"));

  assert.equal(await waiting, 0);
});

test("rejects a process exit before the required output", async () => {
  const tracker = createPtyCompletionOutputTracker("ready");

  await assert.rejects(
    waitForPtyCompletion(Promise.resolve(0), never, tracker),
    /process exited with status 0 before required completion output/,
  );
});

test("rejects an expired deadline before the required output", async () => {
  const tracker = createPtyCompletionOutputTracker("ready");
  const timeout = new Promise<number>((_resolve, reject) => {
    setTimeout(() => reject(new Error("process timed out after 10ms")), 0);
  });

  await assert.rejects(
    waitForPtyCompletion(never, timeout, tracker),
    /process timed out after 10ms/,
  );
});

test("validates the optional completion literal", () => {
  assert.equal(validatePtyCompletionOutput(undefined), undefined);
  assert.equal(validatePtyCompletionOutput(null), undefined);
  assert.equal(validatePtyCompletionOutput("ready"), "ready");

  for (const value of ["", "ready\0now", 1, "x".repeat(4097)]) {
    assert.throws(() => validatePtyCompletionOutput(value));
  }
});
