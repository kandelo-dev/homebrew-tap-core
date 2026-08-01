import { describe, expect, it } from "vitest";

import { parseConfig, resolveGuestProgram } from "../run-browser-wasm.ts";

function config(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    argv: [],
    argv0: "js",
    env: {},
    timeoutMs: 1_000,
    allowStderr: false,
    mergeStderr: false,
    expectedStatus: 0,
    launchCount: 1,
    ...overrides,
  });
}

describe("formula browser guest executable path", () => {
  it("preserves the default staging path when no override is supplied", () => {
    expect(resolveGuestProgram(
      { argv0: "python3" },
      ["/dev", "/proc", "/tmp"],
      {},
      {},
    )).toBe("/usr/local/bin/python3");
  });

  it("accepts an explicit normalized installed path", () => {
    expect(resolveGuestProgram(
      { argv0: "python3", guestProgram: "/home/linuxbrew/.linuxbrew/opt/python/bin/python3" },
      ["/dev", "/proc", "/tmp"],
      {},
      {},
    )).toBe("/home/linuxbrew/.linuxbrew/opt/python/bin/python3");
  });

  it("rejects traversal, overlaid roots, and staged-file collisions", () => {
    expect(() => resolveGuestProgram(
      { argv0: "python3", guestProgram: "/opt/python/../bin/python3" },
      ["/tmp"],
      {},
      {},
    )).toThrow(/absolute and normalized/);
    expect(() => resolveGuestProgram(
      { argv0: "python3", guestProgram: "/tmp/python3" },
      ["/tmp"],
      {},
      {},
    )).toThrow(/hidden by the \/tmp runtime mount/);
    expect(() => resolveGuestProgram(
      { argv0: "python3", guestProgram: "/opt/python/bin/python3" },
      [],
      { "/opt/python/bin/python3": "/host/python3" },
      {},
    )).toThrow(/both the formula executable and a staged file/);
    expect(() => resolveGuestProgram(
      { argv0: "python3", guestProgram: "/opt/python/bin/python3" },
      [],
      {},
      { "/opt/python/bin/python3": "/host/python3" },
    )).toThrow(/both the formula executable and a staged file/);
  });
});

describe("formula browser repeated-launch contract", () => {
  it("accepts the bounded launch and process-memory fields", () => {
    expect(parseConfig(config({
      launchCount: 7,
      maxProcessMemoryBytes: 512 * 1024 * 1024,
    }))).toMatchObject({
      launchCount: 7,
      maxProcessMemoryBytes: 512 * 1024 * 1024,
    });
  });

  it.each([
    ["zero launches", { launchCount: 0 }, /invalid formula browser launch count/],
    ["too many launches", { launchCount: 17 }, /invalid formula browser launch count/],
    ["fractional launches", { launchCount: 1.5 }, /invalid formula browser launch count/],
    ["zero memory", { maxProcessMemoryBytes: 0 }, /invalid formula browser process memory limit/],
    [
      "oversized memory",
      { maxProcessMemoryBytes: 1_073_741_825 },
      /invalid formula browser process memory limit/,
    ],
  ])("rejects %s", (_label, overrides, pattern) => {
    expect(() => parseConfig(config(overrides))).toThrow(pattern);
  });
});
