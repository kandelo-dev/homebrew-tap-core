import { spawn, type ChildProcess } from "node:child_process";
import { createRequire } from "node:module";
import { createServer } from "node:net";
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
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { StringDecoder } from "node:string_decoder";
import { fileURLToPath, pathToFileURL } from "node:url";

import { rootfsSizeForStagedBytes, validateGuestPath } from "./rootfs-size.ts";

const O_WRONLY = 0x0001;
const O_CREAT = 0x0040;
const O_TRUNC = 0x0200;
const S_IFMT = 0xf000;
const S_IFDIR = 0x4000;

interface RunnerConfig {
  argv: string[];
  guestFiles: Record<string, string>;
  minWrites: number;
  minNonBlankPixels: number;
  timeoutMs: number;
}

interface FramebufferSmokeResult {
  binds: number;
  writes: number;
  writeBytes: number;
  width: number;
  height: number;
  format: string | null;
  nonBlankPixels: number;
  exited: boolean;
  exitCode: number | null;
  stdout: string;
  stderr: string;
}

interface WritableRootfs {
  mkdir(path: string, mode: number): void;
  stat(path: string): { mode: number; size: number };
  open(path: string, flags: number, mode: number): number;
  write(
    fd: number,
    data: Uint8Array,
    offset: number | null,
    length: number,
  ): number;
  close(fd: number): void;
}

interface ChildProcessLog {
  stdout: string[];
  stderr: string[];
}

const supportDir = dirname(fileURLToPath(import.meta.url));

function parseConfig(value: string): RunnerConfig {
  const parsed = JSON.parse(value) as Partial<RunnerConfig>;
  if (
    !Array.isArray(parsed.argv) ||
    !parsed.argv.every((arg) => typeof arg === "string")
  ) {
    throw new Error("framebuffer argv must be a JSON string array");
  }
  if (!parsed.guestFiles || typeof parsed.guestFiles !== "object") {
    throw new Error("framebuffer guestFiles must be a JSON object");
  }
  const guestFiles: Record<string, string> = {};
  for (const [guestPath, hostPath] of Object.entries(parsed.guestFiles)) {
    validateGuestPath(guestPath, []);
    if (typeof hostPath !== "string") {
      throw new Error(`guest file source does not exist: ${hostPath}`);
    }
    const absoluteHostPath = resolve(hostPath);
    if (!existsSync(absoluteHostPath) || !statSync(absoluteHostPath).isFile()) {
      throw new Error(`guest file source is not a file: ${absoluteHostPath}`);
    }
    guestFiles[guestPath] = absoluteHostPath;
  }
  if (!Number.isSafeInteger(parsed.minWrites) || (parsed.minWrites ?? 0) < 1) {
    throw new Error(`invalid minimum write count: ${String(parsed.minWrites)}`);
  }
  if (
    !Number.isSafeInteger(parsed.minNonBlankPixels) ||
    (parsed.minNonBlankPixels ?? 0) < 1
  ) {
    throw new Error(
      `invalid minimum nonblank pixel count: ${String(parsed.minNonBlankPixels)}`,
    );
  }
  if (!Number.isSafeInteger(parsed.timeoutMs) || (parsed.timeoutMs ?? 0) < 1) {
    throw new Error(`invalid timeout: ${String(parsed.timeoutMs)}`);
  }
  return { ...parsed, guestFiles } as RunnerConfig;
}

async function availablePort(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("could not allocate a browser test port"));
        return;
      }
      server.close((error) =>
        error ? reject(error) : resolvePort(address.port),
      );
    });
  });
}

function captureChildProcessLog(process: ChildProcess): ChildProcessLog {
  const log: ChildProcessLog = { stdout: [], stderr: [] };
  const stdoutDecoder = new StringDecoder("utf8");
  const stderrDecoder = new StringDecoder("utf8");
  process.stdout?.on("data", (data: Buffer) =>
    log.stdout.push(stdoutDecoder.write(data)),
  );
  process.stderr?.on("data", (data: Buffer) =>
    log.stderr.push(stderrDecoder.write(data)),
  );
  process.stdout?.once("end", () => log.stdout.push(stdoutDecoder.end()));
  process.stderr?.once("end", () => log.stderr.push(stderrDecoder.end()));
  return log;
}

function formatChildProcessLog(log: ChildProcessLog): string {
  return `stdout:\n${log.stdout.join("")}\nstderr:\n${log.stderr.join("")}`;
}

async function waitForVite(
  url: string,
  process: ChildProcess,
  log: ChildProcessLog,
): Promise<void> {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (process.exitCode !== null) {
      throw new Error(
        `Vite exited with ${process.exitCode}: ${formatChildProcessLog(log).slice(-4000)}`,
      );
    }
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Server is still starting.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(
    `Vite did not start within 30 seconds: ${formatChildProcessLog(log).slice(-4000)}`,
  );
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

  // Homebrew runs formula tests with HOME set to the isolated test directory.
  // Playwright's downloaded browser remains beside Homebrew's real cache, so
  // derive that stable location without escaping the formula sandbox for any
  // writes. Explicit PLAYWRIGHT_BROWSERS_PATH and channel overrides still win.
  const homebrewCache = process.env.HOMEBREW_CACHE;
  if (!homebrewCache) return;
  const playwrightCache = resolve(dirname(homebrewCache), "ms-playwright");
  if (existsSync(playwrightCache)) {
    process.env.PLAYWRIGHT_BROWSERS_PATH = playwrightCache;
  }
}

function writeGuestFile(
  rootfs: WritableRootfs,
  guestPath: string,
  bytes: Uint8Array,
  mode: number,
): void {
  const parts = guestPath.split("/").filter(Boolean);
  let parent = "";
  for (let index = 0; index < parts.length - 1; index++) {
    parent += `/${parts[index]}`;
    try {
      rootfs.mkdir(parent, 0o755);
    } catch (error) {
      if ((rootfs.stat(parent).mode & S_IFMT) !== S_IFDIR) throw error;
    }
  }

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
      if (written <= 0) {
        throw new Error(`short write while staging guest file: ${guestPath}`);
      }
      offset += written;
    }
  } finally {
    rootfs.close(fd);
  }
}

async function buildVfs(
  root: string,
  programPath: string,
  config: RunnerConfig,
  imagePath: string,
  guestProgram: string,
): Promise<string> {
  const [
    { tryResolveBinary },
    { MemoryFileSystem },
    { DEFAULT_MOUNT_SPEC },
    { ABI_VERSION },
  ] = await Promise.all([
    import(pathToFileURL(join(root, "host/src/binary-resolver.ts")).href),
    import(pathToFileURL(join(root, "host/src/vfs/memory-fs.ts")).href),
    import(pathToFileURL(join(root, "host/src/vfs/default-mounts.ts")).href),
    import(pathToFileURL(join(root, "host/src/generated/abi.ts")).href),
  ]);

  const rootfsPath =
    tryResolveBinary("rootfs.vfs") ??
    tryResolveBinary("programs/rootfs.vfs") ??
    [
      join(root, "host/wasm/rootfs.vfs"),
      join(root, "local-binaries/rootfs.vfs"),
      join(root, "binaries/rootfs.vfs"),
    ].find(existsSync);
  if (!rootfsPath) {
    throw new Error(
      "rootfs.vfs not found; build or fetch the Kandelo rootfs before testing",
    );
  }

  const rootfsBytes = new Uint8Array(readFileSync(rootfsPath));
  MemoryFileSystem.assertImageKernelAbi(
    rootfsBytes,
    ABI_VERSION,
    "formula framebuffer rootfs",
  );
  if (Object.prototype.hasOwnProperty.call(config.guestFiles, guestProgram)) {
    throw new Error(
      `guest path is both the framebuffer executable and a staged file: ${guestProgram}`,
    );
  }
  const stagedFiles = [
    {
      guestPath: guestProgram,
      bytes: new Uint8Array(readFileSync(programPath)),
      mode: 0o755,
    },
    ...Object.entries(config.guestFiles).map(([guestPath, hostPath]) => ({
      guestPath,
      bytes: new Uint8Array(readFileSync(hostPath)),
      mode: 0o644,
    })),
  ];
  const overlaidRoots = [
    ...DEFAULT_MOUNT_SPEC.filter(
      (mount: { source: string }) => mount.source !== "image",
    ).map((mount: { path: string }) => mount.path),
    "/dev",
    "/proc",
  ];
  for (const entry of stagedFiles)
    validateGuestPath(entry.guestPath, overlaidRoots);
  const stagedBytes = stagedFiles.reduce(
    (total, entry) => total + entry.bytes.byteLength,
    rootfsBytes.byteLength,
  );
  const fs = MemoryFileSystem.fromImage(rootfsBytes).rebaseToNewFileSystem(
    rootfsSizeForStagedBytes(stagedBytes),
  );
  for (const entry of stagedFiles) {
    writeGuestFile(fs, entry.guestPath, entry.bytes, entry.mode);
    const stat = fs.stat(entry.guestPath);
    if (stat.size !== entry.bytes.byteLength) {
      throw new Error(
        `framebuffer VFS file size mismatch for ${entry.guestPath}: ` +
          `${stat.size} != ${entry.bytes.byteLength}`,
      );
    }
  }
  const image = await fs.saveImage({
    metadata: {
      version: 1,
      kernelAbi: ABI_VERSION,
      createdBy: "Kandelo/formula_support/run-framebuffer-wasm.ts",
    },
  });
  writeFileSync(imagePath, image);
  return rootfsPath;
}

async function resolveKernelWasm(root: string): Promise<string> {
  const { tryResolveBinary } = await import(
    pathToFileURL(join(root, "host/src/binary-resolver.ts")).href
  );
  const path =
    tryResolveBinary("kernel.wasm") ??
    [
      join(root, "local-binaries/kernel.wasm"),
      join(root, "binaries/kernel.wasm"),
      join(root, "host/wasm/kernel.wasm"),
      join(root, "host/wasm/kandelo-kernel.wasm"),
    ].find(existsSync);
  if (!path) {
    throw new Error(
      "kernel.wasm not found; build or fetch the Kandelo kernel before testing",
    );
  }
  return path;
}

async function main(): Promise<void> {
  const [rootArg, programArg, configArg] = process.argv.slice(2);
  if (!rootArg || !programArg || !configArg) {
    throw new Error(
      "usage: run-framebuffer-wasm.ts <kandelo-root> <program.wasm> <config-json>",
    );
  }
  const root = resolve(rootArg);
  const programPath = resolve(programArg);
  if (!existsSync(programPath))
    throw new Error(`program does not exist: ${programPath}`);
  const config = parseConfig(configArg);
  const browserDemoDir = join(root, "apps/browser-demos");
  const pageDir = mkdtempSync(join(tmpdir(), "kandelo-formula-fb-"));
  const publicDir = join(pageDir, "public");
  const imagePath = join(publicDir, "formula.vfs");
  const guestProgram = "/usr/local/bin/kandelo-formula-program";
  const port = await availablePort();
  const urlBase = `http://127.0.0.1:${port}`;
  let vite: ChildProcess | null = null;
  let browser: import("playwright").Browser | null = null;

  try {
    mkdirSync(publicDir, { recursive: true });
    copyFileSync(
      join(supportDir, "framebuffer-smoke-page.html"),
      join(pageDir, "index.html"),
    );
    copyFileSync(
      join(supportDir, "framebuffer-smoke-page.ts"),
      join(pageDir, "main.ts"),
    );
    const rootfsPath = await buildVfs(
      root,
      programPath,
      config,
      imagePath,
      guestProgram,
    );
    const kernelWasmPath = await resolveKernelWasm(root);

    vite = spawn(
      "npx",
      [
        "vite",
        pageDir,
        "--config",
        join(supportDir, "framebuffer-vite.config.ts"),
        "--host",
        "127.0.0.1",
        "--port",
        String(port),
        "--strictPort",
      ],
      {
        cwd: browserDemoDir,
        env: {
          ...process.env,
          KANDELO_FORMULA_BROWSER_ROOT: root,
          KANDELO_FORMULA_BROWSER_PAGE_ROOT: pageDir,
          KANDELO_FORMULA_BROWSER_KERNEL_WASM: kernelWasmPath,
          KANDELO_FORMULA_BROWSER_ROOTFS_VFS: rootfsPath,
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    const viteLog = captureChildProcessLog(vite);
    await waitForVite(`${urlBase}/`, vite, viteLog);

    configurePlaywrightBrowserPath();
    const requireFromBrowserApp = createRequire(
      join(browserDemoDir, "package.json"),
    );
    const { chromium } = requireFromBrowserApp(
      "playwright",
    ) as typeof import("playwright");
    browser = await chromium.launch({
      channel: process.env.KANDELO_PLAYWRIGHT_CHANNEL || "chromium",
      headless: true,
    });
    const context = await browser.newContext();
    try {
      const page = await context.newPage();
      const pageErrors: string[] = [];
      const consoleErrors: string[] = [];
      page.on("pageerror", (error) => pageErrors.push(error.message));
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });
      page.on("requestfailed", (request) => {
        pageErrors.push(
          `${request.url()}: ${request.failure()?.errorText ?? "request failed"}`,
        );
      });
      await page.goto(`${urlBase}/`, {
        waitUntil: "domcontentloaded",
        timeout: 60_000,
      });
      try {
        await page.waitForFunction(
          () =>
            (window as unknown as { __kandeloFramebufferReady?: boolean })
              .__kandeloFramebufferReady === true,
          undefined,
          { timeout: 60_000 },
        );
      } catch (error) {
        throw new Error(
          `framebuffer browser page did not initialize: ${JSON.stringify({ pageErrors, consoleErrors, vite: formatChildProcessLog(viteLog).slice(-4_000) })}`,
          { cause: error },
        );
      }
      const canvas = page.locator("#framebuffer");
      const blankScreenshot = await canvas.screenshot();
      const result = await page.evaluate(
        async ({ argv, minWrites, timeoutMs, vfsUrl }) =>
          (
            window as unknown as {
              __runKandeloFramebufferSmoke: (
                request: unknown,
              ) => Promise<FramebufferSmokeResult>;
            }
          ).__runKandeloFramebufferSmoke({
            argv,
            minWrites,
            timeoutMs,
            vfsUrl,
          }),
        {
          argv: [guestProgram, ...config.argv],
          minWrites: config.minWrites,
          timeoutMs: config.timeoutMs,
          vfsUrl: `${urlBase}/formula.vfs`,
        },
      );
      const renderedScreenshot = await canvas.screenshot();

      if (
        result.binds < 1 ||
        result.writes < config.minWrites ||
        result.writeBytes < 1 ||
        result.width < 1 ||
        result.height < 1 ||
        !result.format ||
        result.format === "unknown" ||
        result.nonBlankPixels < config.minNonBlankPixels ||
        result.exited ||
        result.stderr.length > 0 ||
        blankScreenshot.equals(renderedScreenshot) ||
        pageErrors.length > 0 ||
        consoleErrors.length > 0
      ) {
        throw new Error(
          `framebuffer evidence missing: ${JSON.stringify({
            ...result,
            screenshotBytes: renderedScreenshot.byteLength,
            changedFromBlank: !blankScreenshot.equals(renderedScreenshot),
            requiredMinWrites: config.minWrites,
            requiredMinNonBlankPixels: config.minNonBlankPixels,
            pageErrors,
            consoleErrors,
          })}`,
        );
      }
      process.stdout.write(
        `kandelo-framebuffer-ok binds=${result.binds} writes=${result.writes} ` +
          `bytes=${result.writeBytes} size=${result.width}x${result.height} ` +
          `format=${result.format ?? "unknown"} nonblank=${result.nonBlankPixels} ` +
          `screenshot-bytes=${renderedScreenshot.byteLength}\n`,
      );
    } finally {
      await context.close();
    }
  } finally {
    await browser?.close().catch(() => {});
    if (vite) await stopProcess(vite);
    rmSync(pageDir, { recursive: true, force: true });
  }
}

void main().catch((error: unknown) => {
  console.error(
    error instanceof Error ? (error.stack ?? error.message) : String(error),
  );
  process.exitCode = 1;
});
