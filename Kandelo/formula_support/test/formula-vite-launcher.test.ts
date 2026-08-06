import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

import {
  FORMULA_VITE_CLI_RELATIVE,
  formulaViteInvocation,
  resolveFormulaViteCli,
} from "../formula-vite-launcher.ts";

interface RuntimeFixture {
  base: string;
  root: string;
  viteCli: string;
}

const fixtureRoots: string[] = [];

function runtimeFixture(createCli = true): RuntimeFixture {
  const base = realpathSync(mkdtempSync(join(tmpdir(), "kandelo-formula-vite-")));
  fixtureRoots.push(base);
  const root = join(base, "runtime");
  const viteCli = join(root, FORMULA_VITE_CLI_RELATIVE);
  mkdirSync(dirname(viteCli), { recursive: true });
  if (createCli) writeFileSync(viteCli, "#!/usr/bin/env node\n");
  return { base, root, viteCli };
}

afterEach(() => {
  for (const root of fixtureRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("sealed Formula Vite authority", () => {
  it("launches the exact sealed CLI through the current Node executable", () => {
    const fixture = runtimeFixture();
    const invocation = formulaViteInvocation({
      kandeloRoot: fixture.root,
      pageRoot: "/tmp/formula-page",
      configPath: "/tap/browser-vite.config.ts",
      port: 43123,
      cwd: "/sealed/formula runtime/apps/browser-demos",
      environment: {
        KANDELO_FORMULA_VITE_CLI: fixture.viteCli,
        KANDELO_FORMULA_BROWSER_ROOT: fixture.root,
      },
    });

    expect(invocation).toEqual({
      executable: process.execPath,
      arguments: [
        fixture.viteCli,
        "/tmp/formula-page",
        "--config",
        "/tap/browser-vite.config.ts",
        "--host",
        "127.0.0.1",
        "--port",
        "43123",
        "--strictPort",
      ],
      environment: {
        KANDELO_FORMULA_VITE_CLI: fixture.viteCli,
        KANDELO_FORMULA_BROWSER_ROOT: fixture.root,
      },
    });
  });

  it("strips Node injection variables at the final child boundary", () => {
    const fixture = runtimeFixture();
    const environment = {
      KANDELO_FORMULA_VITE_CLI: fixture.viteCli,
      KANDELO_FORMULA_BROWSER_ROOT: fixture.root,
      NODE_OPTIONS: "--import=/caller/mutable/preload.mjs",
      NODE_PATH: "/caller/mutable/node_modules",
    };
    const invocation = formulaViteInvocation({
      kandeloRoot: fixture.root,
      pageRoot: "/tmp/formula-page",
      configPath: "/tap/browser-vite.config.ts",
      port: 43123,
      cwd: "/sealed/formula runtime/apps/browser-demos",
      environment,
    });

    expect(invocation.environment).toEqual({
      KANDELO_FORMULA_VITE_CLI: fixture.viteCli,
      KANDELO_FORMULA_BROWSER_ROOT: fixture.root,
    });
    expect(environment.NODE_OPTIONS).toBe("--import=/caller/mutable/preload.mjs");
    expect(environment.NODE_PATH).toBe("/caller/mutable/node_modules");
  });

  it("rejects missing authority and a missing exact CLI", () => {
    const fixture = runtimeFixture(false);

    expect(() => resolveFormulaViteCli(fixture.root, {}))
      .toThrow(/KANDELO_FORMULA_VITE_CLI is required/);
    expect(() => resolveFormulaViteCli(fixture.root, {
      KANDELO_FORMULA_VITE_CLI: fixture.viteCli,
    })).toThrow(/is unavailable/);
  });

  it("rejects relative authority and a non-file exact entry", () => {
    const fixture = runtimeFixture(false);

    expect(() => resolveFormulaViteCli(fixture.root, {
      KANDELO_FORMULA_VITE_CLI: FORMULA_VITE_CLI_RELATIVE,
    })).toThrow(/must be an absolute normalized path/);

    mkdirSync(fixture.viteCli);
    expect(() => resolveFormulaViteCli(fixture.root, {
      KANDELO_FORMULA_VITE_CLI: fixture.viteCli,
    })).toThrow(/regular non-symlink file/);
  });

  it("rejects a symlink in place of the exact CLI", () => {
    const fixture = runtimeFixture(false);
    const target = join(fixture.base, "vite-target.js");
    writeFileSync(target, "#!/usr/bin/env node\n");
    symlinkSync(target, fixture.viteCli);

    expect(() => resolveFormulaViteCli(fixture.root, {
      KANDELO_FORMULA_VITE_CLI: fixture.viteCli,
    })).toThrow(/regular non-symlink file/);
  });

  it("rejects an exact lexical path whose ancestor escapes by symlink", () => {
    const fixture = runtimeFixture(false);
    const nodeModules = join(fixture.root, "node_modules");
    const escapedModules = join(fixture.base, "outside-node-modules");
    const escapedCli = join(escapedModules, "vite/bin/vite.js");
    rmSync(nodeModules, { recursive: true });
    mkdirSync(dirname(escapedCli), { recursive: true });
    writeFileSync(escapedCli, "#!/usr/bin/env node\n");
    symlinkSync(escapedModules, nodeModules);

    expect(() => resolveFormulaViteCli(fixture.root, {
      KANDELO_FORMULA_VITE_CLI: fixture.viteCli,
    })).toThrow(/path is not canonical/);
  });

  it("rejects a normalized path that escapes the sealed runtime", () => {
    const fixture = runtimeFixture();
    const escaped = join(fixture.base, "outside", "vite.js");
    mkdirSync(dirname(escaped), { recursive: true });
    writeFileSync(escaped, "#!/usr/bin/env node\n");

    expect(() => resolveFormulaViteCli(fixture.root, {
      KANDELO_FORMULA_VITE_CLI: escaped,
    })).toThrow(/must select the sealed runtime Vite CLI/);
  });

  it("rejects a different file inside the sealed runtime", () => {
    const fixture = runtimeFixture();
    const wrong = join(fixture.root, "node_modules/vite/bin/other.js");
    writeFileSync(wrong, "#!/usr/bin/env node\n");

    expect(() => resolveFormulaViteCli(fixture.root, {
      KANDELO_FORMULA_VITE_CLI: wrong,
    })).toThrow(/must select the sealed runtime Vite CLI/);
  });
});

describe("Formula browser Vite call sites", () => {
  it("routes every browser helper through the shared exact launcher", () => {
    const supportDir = dirname(dirname(fileURLToPath(import.meta.url)));
    const helpers = {
      "run-browser-wasm.ts": "browser-vite.config.ts",
      "run-framebuffer-wasm.ts": "framebuffer-vite.config.ts",
      "run-kms-browser-wasm.ts": "kms-vite.config.ts",
    };
    for (const [name, configName] of Object.entries(helpers)) {
      const source = readFileSync(join(supportDir, name), "utf8");
      expect(source, name).toContain(
        'import { spawnFormulaVite } from "./formula-vite-launcher.ts";',
      );
      expect(source.match(/spawnFormulaVite\(\{/g), name).toHaveLength(1);
      expect(source, name).toContain(
        `configPath: join(supportDir, "${configName}")`,
      );
      expect(source, name).not.toMatch(/\bspawn\(\s*["']npx["']/);
    }
  });
});
