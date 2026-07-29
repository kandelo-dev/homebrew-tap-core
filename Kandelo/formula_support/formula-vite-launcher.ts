import { spawn, type ChildProcess } from "node:child_process";
import { lstatSync, realpathSync, type Stats } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";

export const FORMULA_VITE_CLI_RELATIVE = "node_modules/vite/bin/vite.js";

interface FormulaViteLaunchOptions {
  kandeloRoot: string;
  pageRoot: string;
  configPath: string;
  port: number;
  cwd: string;
  environment: NodeJS.ProcessEnv;
}

export interface FormulaViteInvocation {
  executable: string;
  arguments: string[];
  environment: NodeJS.ProcessEnv;
}

export function resolveFormulaViteCli(
  kandeloRoot: string,
  environment: Readonly<NodeJS.ProcessEnv>,
): string {
  const selected = environment.KANDELO_FORMULA_VITE_CLI;
  if (!selected) {
    throw new Error("KANDELO_FORMULA_VITE_CLI is required");
  }
  if (!isAbsolute(selected) || resolve(selected) !== selected) {
    throw new Error("KANDELO_FORMULA_VITE_CLI must be an absolute normalized path");
  }

  const root = resolve(kandeloRoot);
  const expected = join(root, FORMULA_VITE_CLI_RELATIVE);
  if (selected !== expected) {
    throw new Error(
      `KANDELO_FORMULA_VITE_CLI must select the sealed runtime Vite CLI: ${expected}`,
    );
  }

  let state: Stats;
  try {
    state = lstatSync(selected);
  } catch {
    throw new Error(`sealed Formula Vite CLI is unavailable: ${selected}`);
  }
  if (state.isSymbolicLink() || !state.isFile()) {
    throw new Error(`sealed Formula Vite CLI must be a regular non-symlink file: ${selected}`);
  }
  if (realpathSync(selected) !== selected) {
    throw new Error(`sealed Formula Vite CLI path is not canonical: ${selected}`);
  }
  return selected;
}

export function formulaViteInvocation(
  options: FormulaViteLaunchOptions,
): FormulaViteInvocation {
  const viteCli = resolveFormulaViteCli(
    options.kandeloRoot,
    options.environment,
  );
  const environment = { ...options.environment };
  // WHY: the outer publisher starts Formula tests with a clean environment,
  // but evaluated Formula Ruby can repopulate these variables before calling
  // a browser helper. Node consumes them before the exact Vite CLI starts, so
  // they would restore mutable module or preload authority at the last hop.
  delete environment.NODE_OPTIONS;
  delete environment.NODE_PATH;
  return {
    executable: process.execPath,
    arguments: [
      viteCli,
      options.pageRoot,
      "--config",
      options.configPath,
      "--host",
      "127.0.0.1",
      "--port",
      String(options.port),
      "--strictPort",
    ],
    environment,
  };
}

export function spawnFormulaVite(
  options: FormulaViteLaunchOptions,
): ChildProcess {
  const invocation = formulaViteInvocation(options);
  // WHY: npx and PATH can select or install mutable code. The parent Formula
  // support layer derives one CLI path from its already-authenticated runtime,
  // and this launcher revalidates that exact file before Node executes it.
  return spawn(invocation.executable, invocation.arguments, {
    cwd: options.cwd,
    env: invocation.environment,
    stdio: ["ignore", "pipe", "pipe"],
  });
}
