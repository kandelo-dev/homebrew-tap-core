import assert from "node:assert/strict";
import test from "node:test";

import { createPtyOutputReadiness } from "../pty-output-readiness.ts";

const encoder = new TextEncoder();

test("matches readiness text split across PTY output chunks", async () => {
  const readiness = createPtyOutputReadiness("COMMIT_EDITMSG");
  let ready = false;
  const waiting = readiness.wait().then(() => {
    ready = true;
  });

  readiness.observe(encoder.encode("\u001b[2JCOMMIT_"));
  await Promise.resolve();
  assert.equal(ready, false);

  readiness.observe(encoder.encode("EDITMSG\u001b[1;1H"));
  await waiting;
  assert.equal(ready, true);
});

test("retains readiness observed before the input task starts waiting", async () => {
  const readiness = createPtyOutputReadiness("editor ready");
  readiness.observe(encoder.encode("terminal: editor ready\r\n"));

  await readiness.wait();
});

test("rejects an empty readiness marker", () => {
  assert.throws(
    () => createPtyOutputReadiness(""),
    /PTY input readiness text must not be empty/,
  );
});
