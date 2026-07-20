import { spawn, type ChildProcess } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, posix, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  rootfsSizeForStagedBytes,
  rootfsUsedBytes,
  validateGuestPath,
} from "./rootfs-size.ts";

interface RunnerConfig {
  argv: string[];
  argv0: string;
  env: Record<string, string>;
  timeoutMs: number;
  allowStderr: boolean;
  mergeStderr: boolean;
  expectedStatus: number;
}

interface BrowserSmokeResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  mergedOutput: string;
}

interface PageRunnerConfig extends RunnerConfig {
  guestProgram: string;
  vfsUrl: string;
}

const supportDir = dirname(fileURLToPath(import.meta.url));
const O_WRONLY = 0x0001;
const O_CREAT = 0x0040;
const O_TRUNC = 0x0200;

interface WritableRootfs {
  open(path: string, flags: number, mode: number): number;
  write(fd: number, data: Uint8Array, offset: number | null, length: number): number;
  close(fd: number): void;
}

function writeStagedFile(
  rootfs: WritableRootfs,
  guestPath: string,
  bytes: Uint8Array,
  mode: number,
): void {
  const fd = rootfs.open(guestPath, O_WRONLY | O_CREAT | O_TRUNC, mode);
  try {
    let offset = 0;
    while (offset < bytes.byteLength) {
      const written = rootfs.write(
        fd,
        bytes.subarray(offset),
        null,
        bytes.byteLength - offset,
      );
      if (written <= 0) throw new Error(`short write while staging browser file: ${guestPath}`);
      offset += written;
    }
  } finally {
    rootfs.close(fd);
  }
}

function parseConfig(text: string): RunnerConfig {
  const value = JSON.parse(text) as Partial<RunnerConfig>;
  if (!Array.isArray(value.argv) || !value.argv.every((arg) => typeof arg === "string")) {
    throw new Error("formula browser argv must be a string array");
  }
  if (
    typeof value.argv0 !== "string" ||
    value.argv0.length === 0 ||
    value.argv0.includes("/") ||
    value.argv0.includes("\0") ||
    value.argv0 === "." ||
    value.argv0 === ".."
  ) {
    throw new Error(`invalid formula browser argv0: ${String(value.argv0)}`);
  }
  if (!value.env || typeof value.env !== "object" || Array.isArray(value.env)) {
    throw new Error("formula browser env must be an object");
  }
  if (!Object.entries(value.env).every(([key, item]) => key.length > 0 && typeof item === "string")) {
    throw new Error("formula browser env values must be strings");
  }
  if (!Number.isSafeInteger(value.timeoutMs) || (value.timeoutMs ?? 0) < 1) {
    throw new Error(`invalid formula browser timeout: ${String(value.timeoutMs)}`);
  }
  if (typeof value.allowStderr !== "boolean") {
    throw new Error("formula browser allowStderr must be boolean");
  }
  if (typeof value.mergeStderr !== "boolean") {
    throw new Error("formula browser mergeStderr must be boolean");
  }
  if (
    !Number.isSafeInteger(value.expectedStatus) ||
    (value.expectedStatus ?? -1) < 0 ||
    (value.expectedStatus ?? -1) > 255
  ) {
    throw new Error(`invalid formula browser expected status: ${String(value.expectedStatus)}`);
  }
  return value as RunnerConfig;
}

async function availablePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("could not allocate a formula browser test port"));
        return;
      }
      server.close((error) => error ? reject(error) : resolvePort(address.port));
    });
  });
}

async function waitForVite(url: string, process: ChildProcess, log: string[]): Promise<void> {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (process.exitCode !== null) {
      throw new Error(`Vite exited with ${process.exitCode}: ${log.join("").slice(-4_000)}`);
    }
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Vite is still starting.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(`Vite did not start within 30 seconds: ${log.join("").slice(-4_000)}`);
}

async function stopProcess(process: ChildProcess): Promise<void> {
  if (process.exitCode !== null) return;
  process.kill("SIGTERM");
  await new Promise<void>((resolveExit) => {
    const timer = setTimeout(() => {
      process.kill("SIGKILL");
      resolveExit();
    }, 2_000);
    process.once("exit", () => {
      clearTimeout(timer);
      resolveExit();
    });
  });
}

function configurePlaywrightBrowserPath(): void {
  if (process.env.PLAYWRIGHT_BROWSERS_PATH) return;

  // Formula tests isolate HOME, while the downloaded Chromium remains beside
  // Homebrew's real cache. Explicit browser-path and channel overrides win.
  const homebrewCache = process.env.HOMEBREW_CACHE;
  if (!homebrewCache) return;
  const playwrightCache = resolve(dirname(homebrewCache), "ms-playwright");
  if (existsSync(playwrightCache)) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = playwrightCache;
  }
}

async function resolveArtifact(root: string, candidates: string[], label: string): Promise<string> {
  const { tryResolveBinary } = await import(
    pathToFileURL(join(root, "host/src/binary-resolver.ts")).href
  );
  const resolved = candidates
    .map((candidate) => candidate.startsWith("resolve:")
      ? tryResolveBinary(candidate.slice("resolve:".length))
      : join(root, candidate))
    .find((candidate): candidate is string => Boolean(candidate && existsSync(candidate)));
  if (!resolved) throw new Error(`${label} not found; build or fetch Kandelo runtime artifacts`);
  return resolved;
}

async function buildVfs(
  root: string,
  rootfsPath: string,
  programPath: string,
  guestProgram: string,
  guestFiles: Record<string, string>,
  execPrograms: Record<string, string>,
  imagePath: string,
): Promise<void> {
  const [{ MemoryFileSystem }, imageHelpers, { ABI_VERSION }] = await Promise.all([
    import(pathToFileURL(join(root, "host/src/vfs/memory-fs.ts")).href),
    import(pathToFileURL(join(root, "host/src/vfs/image-helpers.ts")).href),
    import(pathToFileURL(join(root, "host/src/generated/abi.ts")).href),
  ]);
  const rootfsBytes = new Uint8Array(readFileSync(rootfsPath));
  MemoryFileSystem.assertImageKernelAbi(rootfsBytes, ABI_VERSION, "formula browser rootfs");
  const sourceFs = MemoryFileSystem.fromImage(rootfsBytes);

  const programBytes = new Uint8Array(readFileSync(programPath));
  const stagedFiles = [
    ...Object.entries(guestFiles).map(([guestPath, hostPath]) => ({
      guestPath,
      hostPath,
      mode: 0o644,
    })),
    ...Object.entries(execPrograms).map(([guestPath, hostPath]) => ({
      guestPath,
      hostPath,
      mode: 0o755,
    })),
  ]
    .sort((left, right) => left.guestPath.localeCompare(right.guestPath))
    .map(({ guestPath, hostPath, mode }) => {
      const absoluteHostPath = resolve(hostPath);
      const stat = statSync(absoluteHostPath);
      if (!stat.isFile()) {
        throw new Error(`formula browser guest source is not a file: ${absoluteHostPath}`);
      }
      return { guestPath, bytes: new Uint8Array(readFileSync(absoluteHostPath)), mode };
    });
  const stagedBytes = stagedFiles.reduce(
    (total, entry) => total + entry.bytes.byteLength,
    programBytes.byteLength + rootfsUsedBytes(sourceFs.statfs("/")),
  );
  const maxByteLength = rootfsSizeForStagedBytes(stagedBytes);
  const buildFs = sourceFs.rebaseToNewFileSystem(maxByteLength);
  for (const { guestPath, bytes, mode } of stagedFiles) {
    imageHelpers.ensureDirRecursive(buildFs, posix.dirname(guestPath));
    writeStagedFile(buildFs, guestPath, bytes, mode);
  }
  imageHelpers.ensureDirRecursive(buildFs, posix.dirname(guestProgram));
  writeStagedFile(buildFs, guestProgram, programBytes, 0o755);
  const image = await buildFs.saveImage({
    metadata: {
      version: 1,
      kernelAbi: ABI_VERSION,
      createdBy: "Kandelo/formula_support/run-browser-wasm.ts",
    },
  });
  const verificationFs = MemoryFileSystem.fromImage(image);
  const programStat = verificationFs.stat(guestProgram);
  if (programStat.size !== programBytes.byteLength) {
    throw new Error(
      `formula browser VFS program size mismatch: ${programStat.size} != ${programBytes.byteLength}`,
    );
  }
  for (const { guestPath, bytes, mode } of stagedFiles) {
    const stat = verificationFs.stat(guestPath);
    if (stat.size !== bytes.byteLength) {
      throw new Error(
        `formula browser VFS guest file size mismatch for ${guestPath}: ${stat.size} != ${bytes.byteLength}`,
      );
    }
    if ((mode & 0o111) !== 0 && (stat.mode & 0o111) === 0) {
      throw new Error(`formula browser VFS executable mode missing for ${guestPath}`);
    }
  }
  writeFileSync(imagePath, image);
}

function readStagedManifest(path: string, label: string): Record<string, string> {
  const manifest = resolve(path);
  const value = JSON.parse(readFileSync(manifest, "utf8")) as unknown;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`formula browser ${label} manifest must be an object`);
  }
  if (!Object.entries(value).every(
    ([guestPath, hostPath]) => guestPath.length > 0 && typeof hostPath === "string",
  )) {
    throw new Error(`formula browser ${label} manifest values must be host paths`);
  }
  return value as Record<string, string>;
}

async function main(): Promise<void> {
  const [rootArg, programArg, configArg, guestFilesManifestArg, execProgramsManifestArg] =
    process.argv.slice(2);
  if (!rootArg || !programArg || !configArg || !guestFilesManifestArg || !execProgramsManifestArg) {
    throw new Error(
      "usage: run-browser-wasm.ts <kandelo-root> <program.wasm> <config-json> " +
        "<guest-files-json> <exec-programs-json>",
    );
  }

  const root = resolve(rootArg);
  const program = resolve(programArg);
  if (!existsSync(program)) throw new Error(`formula Wasm does not exist: ${program}`);
  const config = parseConfig(configArg);
  const guestFiles = readStagedManifest(guestFilesManifestArg, "guest-files");
  const execPrograms = readStagedManifest(execProgramsManifestArg, "exec-programs");
  const browserApp = join(root, "apps/browser-demos");
  const pageRoot = mkdtempSync(join(tmpdir(), "kandelo-formula-browser-"));
  const port = await availablePort();
  const url = `http://127.0.0.1:${port}/`;
  let vite: ChildProcess | null = null;
  let browser: import("playwright").Browser | null = null;

  try {
    copyFileSync(join(supportDir, "browser-smoke-page.html"), join(pageRoot, "index.html"));
    copyFileSync(join(supportDir, "browser-smoke-page.ts"), join(pageRoot, "main.ts"));
    const defaultMountsUrl = pathToFileURL(join(root, "host/src/vfs/default-mounts.ts")).href;
    const { DEFAULT_MOUNT_SPEC } = await import(defaultMountsUrl);
    const overlaidRoots = [
      ...DEFAULT_MOUNT_SPEC.filter(
        (mount: { source: string }) => mount.source !== "image",
      ).map((mount: { path: string }) => mount.path),
      "/dev",
      "/proc",
    ];
    const stagedPaths = [...Object.keys(guestFiles), ...Object.keys(execPrograms)];
    for (const guestPath of stagedPaths) {
      validateGuestPath(guestPath, overlaidRoots);
    }
    for (const guestPath of Object.keys(execPrograms)) {
      if (guestPath in guestFiles) {
        throw new Error(`guest path is both a file and executable: ${guestPath}`);
      }
    }
    const guestProgram = `/usr/local/bin/${config.argv0}`;
    if (guestProgram in guestFiles || guestProgram in execPrograms) {
      throw new Error(`guest path is both the formula executable and a staged file: ${guestProgram}`);
    }
    const kernelWasm = await resolveArtifact(root, [
      "resolve:kernel.wasm", "local-binaries/kernel.wasm", "binaries/kernel.wasm",
      "host/wasm/kernel.wasm", "host/wasm/kandelo-kernel.wasm",
    ], "kernel.wasm");
    const rootfsVfs = await resolveArtifact(root, [
      "resolve:rootfs.vfs", "resolve:programs/rootfs.vfs", "host/wasm/rootfs.vfs",
      "local-binaries/rootfs.vfs", "binaries/rootfs.vfs",
    ], "rootfs.vfs");
    const publicDir = join(pageRoot, "public");
    mkdirSync(publicDir, { recursive: true });
    await buildVfs(
      root,
      rootfsVfs,
      program,
      guestProgram,
      guestFiles,
      execPrograms,
      join(publicDir, "formula.vfs"),
    );
    const pageConfig: PageRunnerConfig = {
      ...config,
      guestProgram,
      vfsUrl: "/formula.vfs",
    };

    const viteLog: string[] = [];
    vite = spawn("npx", [
      "vite", pageRoot, "--config", join(supportDir, "browser-vite.config.ts"),
      "--host", "127.0.0.1", "--port", String(port), "--strictPort",
    ], {
      cwd: browserApp,
      env: {
        ...process.env,
        KANDELO_FORMULA_BROWSER_ROOT: root,
        KANDELO_FORMULA_BROWSER_PAGE_ROOT: pageRoot,
        KANDELO_FORMULA_BROWSER_KERNEL_WASM: kernelWasm,
        KANDELO_FORMULA_BROWSER_ROOTFS_VFS: rootfsVfs,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    vite.stdout?.on("data", (data: Buffer) => viteLog.push(data.toString()));
    vite.stderr?.on("data", (data: Buffer) => viteLog.push(data.toString()));
    await waitForVite(url, vite, viteLog);

    configurePlaywrightBrowserPath();
    const requireFromBrowserApp = createRequire(join(browserApp, "package.json"));
    const { chromium } = requireFromBrowserApp("playwright") as typeof import("playwright");
    const channel = process.env.KANDELO_PLAYWRIGHT_CHANNEL;
    browser = await chromium.launch({ headless: true, ...(channel ? { channel } : {}) });
    const context = await browser.newContext();
    try {
      const page = await context.newPage();
      const pageErrors: string[] = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") pageErrors.push(message.text());
      });
      page.on("requestfailed", (request) => {
        pageErrors.push(`${request.url()}: ${request.failure()?.errorText ?? "request failed"}`);
      });
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60_000 });
      await page.waitForFunction(
        () => (window as unknown as { __kandeloFormulaBrowserReady?: boolean })
          .__kandeloFormulaBrowserReady === true,
        undefined,
        { timeout: 60_000 },
      );

      const result = await page.evaluate(
        (request) => (window as unknown as {
          __runKandeloFormulaBrowserSmoke: (value: PageRunnerConfig) => Promise<BrowserSmokeResult>;
        }).__runKandeloFormulaBrowserSmoke(request),
        pageConfig,
      );
      const unexpectedStderr = !config.allowStderr && !config.mergeStderr && result.stderr.length > 0;
      if (
        result.exitCode !== config.expectedStatus ||
        unexpectedStderr ||
        pageErrors.length > 0
      ) {
        throw new Error(`formula browser smoke failed: ${JSON.stringify({ ...result, pageErrors })}`);
      }
      process.stdout.write(config.mergeStderr ? result.mergedOutput : result.stdout);
      await page.evaluate(() =>
        (window as unknown as { __cleanupKandeloFormulaBrowserSmoke?: () => Promise<void> })
          .__cleanupKandeloFormulaBrowserSmoke?.(),
      );
    } finally {
      await context.close();
    }
  } finally {
    await browser?.close().catch(() => {});
    if (vite) await stopProcess(vite);
    rmSync(pageRoot, { recursive: true, force: true });
  }
}

void main().catch((error: unknown) => {
  console.error(error instanceof Error ? (error.stack ?? error.message) : String(error));
  process.exitCode = 1;
});
