import assert from "node:assert/strict";
import test from "node:test";

import { addDefaultBaseExecPrograms } from "../base-exec-programs.ts";

test("adds resolver-managed Dash as the default guest shell", () => {
  const execPrograms: Record<string, string> = {};
  const requested: string[] = [];

  const result = addDefaultBaseExecPrograms(execPrograms, (relativePath) => {
    requested.push(relativePath);
    return "/trusted/binaries/programs/wasm32/dash.wasm";
  });

  assert.equal(result, execPrograms);
  assert.deepEqual(requested, ["programs/dash.wasm"]);
  assert.deepEqual(execPrograms, {
    "/bin/sh": "/trusted/binaries/programs/wasm32/dash.wasm",
  });
});

test("preserves an explicit guest shell without consulting the resolver", () => {
  const execPrograms = { "/bin/sh": "/formula/custom-shell.wasm" };

  addDefaultBaseExecPrograms(execPrograms, () => {
    throw new Error("resolver must not run for an explicit shell");
  });

  assert.deepEqual(execPrograms, {
    "/bin/sh": "/formula/custom-shell.wasm",
  });
});

test("uses an explicitly staged Dash as the default guest shell", () => {
  const execPrograms = { "/bin/dash": "/formula/fresh-dash.wasm" };

  addDefaultBaseExecPrograms(execPrograms, () => {
    throw new Error("resolver must not run when Dash is already staged");
  });

  assert.deepEqual(execPrograms, {
    "/bin/dash": "/formula/fresh-dash.wasm",
    "/bin/sh": "/formula/fresh-dash.wasm",
  });
});

test("fails closed when the resolver cannot provide Dash", () => {
  assert.throws(
    () =>
      addDefaultBaseExecPrograms({}, () => {
        throw new Error("Binary not found: programs/dash.wasm");
      }),
    /Binary not found: programs\/dash\.wasm/,
  );
});
