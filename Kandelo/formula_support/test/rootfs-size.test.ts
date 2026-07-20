import assert from "node:assert/strict";
import test from "node:test";

import {
  rootfsSizeForStagedBytes,
  rootfsUsedBytes,
  validateGuestPath,
} from "../rootfs-size.ts";

test("keeps an empty formula rootfs at the minimum size", () => {
  assert.equal(rootfsSizeForStagedBytes(0), 2 * 1024 * 1024);
});

test("aligns odd staged payloads to a SharedFS block", () => {
  const stagedBytes = 524_289;
  const rootfsSize = rootfsSizeForStagedBytes(stagedBytes);

  assert.equal(rootfsSize % 4096, 0);
  assert.ok(rootfsSize >= stagedBytes * 2 + 1024 * 1024);
});

test("rejects invalid staged byte counts", () => {
  assert.throws(() => rootfsSizeForStagedBytes(-1));
  assert.throws(() => rootfsSizeForStagedBytes(0.5));
  assert.throws(
    () => rootfsSizeForStagedBytes(Number.MAX_SAFE_INTEGER),
    /too large/,
  );
});

test("derives occupied bytes from rootfs block accounting", () => {
  assert.equal(
    rootfsUsedBytes({ bsize: 4096, blocks: 100, bfree: 75 }),
    102_400,
  );
});

test("rejects invalid rootfs block accounting", () => {
  assert.throws(() => rootfsUsedBytes({ bsize: 0, blocks: 1, bfree: 0 }));
  assert.throws(() => rootfsUsedBytes({ bsize: 4096, blocks: 1, bfree: 2 }));
  assert.throws(() => rootfsUsedBytes({
    bsize: 4096,
    blocks: Number.MAX_SAFE_INTEGER,
    bfree: 0,
  }));
});

test("accepts normalized image-backed guest paths", () => {
  assert.doesNotThrow(() => validateGuestPath("/etc/dinit.d/probe", ["/tmp"]));
});

test("rejects malformed and runtime-overlaid guest paths", () => {
  assert.throws(
    () => validateGuestPath("etc/probe", []),
    /absolute and normalized/,
  );
  assert.throws(
    () => validateGuestPath("/etc/../tmp/probe", []),
    /absolute and normalized/,
  );
  assert.throws(
    () => validateGuestPath("/etc/probe\0hidden", []),
    /absolute and normalized/,
  );
  assert.throws(
    () => validateGuestPath("/tmp", ["/tmp"]),
    /hidden by the \/tmp runtime mount/,
  );
  assert.throws(
    () => validateGuestPath("/tmp/probe", ["/tmp"]),
    /hidden by the \/tmp runtime mount/,
  );
});
