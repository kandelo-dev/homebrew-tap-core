import { describe, expect, it } from "vitest";

import { resolveGuestProgram } from "../run-browser-wasm.ts";
import * as browserRunner from "../run-browser-wasm.ts";

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
      { argv0: "python3", guestProgram: "/opt/kandelo/homebrew/opt/python/bin/python3" },
      ["/dev", "/proc", "/tmp"],
      {},
      {},
    )).toBe("/opt/kandelo/homebrew/opt/python/bin/python3");
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

describe("formula browser runtime artifact selection", () => {
  it("stops after the first resolved artifact instead of probing later package indexes", () => {
    const selection = browserRunner as unknown as {
      resolveArtifactCandidate?: (
        candidates: string[],
        resolveBinary: (name: string) => string | undefined,
        exists: (path: string) => boolean,
      ) => string | undefined;
    };
    expect(selection.resolveArtifactCandidate).toBeTypeOf("function");
    if (!selection.resolveArtifactCandidate) return;

    const resolverCalls: string[] = [];
    const selected = selection.resolveArtifactCandidate(
      ["resolve:rootfs.vfs", "resolve:programs/rootfs.vfs", "/fallback/rootfs.vfs"],
      (name) => {
        resolverCalls.push(name);
        return name === "rootfs.vfs" ? "/sealed/rootfs.vfs" : undefined;
      },
      () => false,
    );

    expect(selected).toBe("/sealed/rootfs.vfs");
    expect(resolverCalls).toEqual(["rootfs.vfs"]);
  });
});
