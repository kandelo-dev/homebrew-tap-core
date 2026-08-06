import type { ChildProcess } from "node:child_process";
import { createRequire } from "node:module";
import { createServer } from "node:net";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, posix, resolve } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";

import { spawnFormulaVite } from "./formula-vite-launcher.ts";

interface RunnerConfig {
  argv: string[];
  minPageFlips: number;
  timeoutMs: number;
}

interface KmsSmokeResult {
  pageFlips: number;
  width: number;
  height: number;
  lastFrameUs: number;
  exited: boolean;
  exitCode: number | null;
  stdout: string;
  stderr: string;
}

interface PixelStats {
  width: number;
  height: number;
  nonUniformPixels: number;
  lumaRange: number;
}

const supportDir = dirname(fileURLToPath(import.meta.url));

function parseConfig(value: string): RunnerConfig {
  const parsed = JSON.parse(value) as Partial<RunnerConfig>;
  if (!Array.isArray(parsed.argv) || !parsed.argv.every((arg) => typeof arg === "string")) {
    throw new Error("KMS argv must be a JSON string array");
  }
  if (!Number.isSafeInteger(parsed.minPageFlips) || (parsed.minPageFlips ?? 0) < 1) {
    throw new Error(`invalid minimum PAGE_FLIP count: ${String(parsed.minPageFlips)}`);
  }
  if (!Number.isSafeInteger(parsed.timeoutMs) || (parsed.timeoutMs ?? 0) < 1) {
    throw new Error(`invalid timeout: ${String(parsed.timeoutMs)}`);
  }
  return parsed as RunnerConfig;
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
      // Server is still starting.
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

async function buildVfs(
  root: string,
  programPath: string,
  imagePath: string,
  guestProgram: string,
): Promise<string> {
  const [{ tryResolveBinary }, { MemoryFileSystem }, imageHelpers, { ABI_VERSION }] =
    await Promise.all([
      import(pathToFileURL(join(root, "host/src/binary-resolver.ts")).href),
      import(pathToFileURL(join(root, "host/src/vfs/memory-fs.ts")).href),
      import(pathToFileURL(join(root, "host/src/vfs/image-helpers.ts")).href),
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
    throw new Error("rootfs.vfs not found; build or fetch the Kandelo rootfs before testing");
  }

  const rootfsBytes = new Uint8Array(readFileSync(rootfsPath));
  MemoryFileSystem.assertImageKernelAbi(rootfsBytes, ABI_VERSION, "formula KMS rootfs");
  const fs = MemoryFileSystem.fromImage(rootfsBytes, {
    maxByteLength: 256 * 1024 * 1024,
  });
  imageHelpers.ensureDirRecursive(fs, posix.dirname(guestProgram));
  imageHelpers.writeVfsBinary(fs, guestProgram, new Uint8Array(readFileSync(programPath)), 0o755);
  const image = await fs.saveImage({
    metadata: {
      version: 1,
      kernelAbi: ABI_VERSION,
      createdBy: "Kandelo/formula_support/run-kms-browser-wasm.ts",
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
    throw new Error("kernel.wasm not found; build or fetch the Kandelo kernel before testing");
  }
  return path;
}

async function readPixelStats(
  page: import("playwright").Page,
  screenshot: Buffer,
): Promise<PixelStats> {
  return page.evaluate(async (pngBase64) => {
    const image = new Image();
    image.src = `data:image/png;base64,${pngBase64}`;
    await image.decode();

    const analysis = document.createElement("canvas");
    analysis.width = image.naturalWidth;
    analysis.height = image.naturalHeight;
    const context = analysis.getContext("2d", { willReadFrequently: true });
    if (!context) throw new Error("could not create screenshot analysis context");
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(0, 0, analysis.width, analysis.height).data;
    const baseRed = pixels[0];
    const baseGreen = pixels[1];
    const baseBlue = pixels[2];
    let nonUniformPixels = 0;
    let minLuma = 255;
    let maxLuma = 0;
    for (let index = 0; index < pixels.length; index += 4) {
      const red = pixels[index];
      const green = pixels[index + 1];
      const blue = pixels[index + 2];
      if (
        Math.abs(red - baseRed) > 3 ||
        Math.abs(green - baseGreen) > 3 ||
        Math.abs(blue - baseBlue) > 3
      ) {
        nonUniformPixels += 1;
      }
      const luma = (red * 54 + green * 183 + blue * 19) >> 8;
      minLuma = Math.min(minLuma, luma);
      maxLuma = Math.max(maxLuma, luma);
    }
    return {
      width: analysis.width,
      height: analysis.height,
      nonUniformPixels,
      lumaRange: maxLuma - minLuma,
    };
  }, screenshot.toString("base64"));
}

async function main(): Promise<void> {
  const [rootArg, programArg, configArg] = process.argv.slice(2);
  if (!rootArg || !programArg || !configArg) {
    throw new Error("usage: run-kms-browser-wasm.ts <kandelo-root> <program.wasm> <config-json>");
  }
  const root = resolve(rootArg);
  const programPath = resolve(programArg);
  if (!existsSync(programPath)) throw new Error(`program does not exist: ${programPath}`);
  const config = parseConfig(configArg);
  const browserDemoDir = join(root, "apps/browser-demos");
  const pageDir = mkdtempSync(join(tmpdir(), "kandelo-formula-kms-"));
  const publicDir = join(pageDir, "public");
  const imagePath = join(publicDir, "formula.vfs");
  const guestProgram = "/usr/local/bin/kandelo-formula-program";
  const port = await availablePort();
  const urlBase = `http://127.0.0.1:${port}`;
  let vite: ChildProcess | null = null;
  let browser: import("playwright").Browser | null = null;

  try {
    mkdirSync(publicDir, { recursive: true });
    copyFileSync(join(supportDir, "kms-smoke-page.html"), join(pageDir, "index.html"));
    copyFileSync(join(supportDir, "kms-smoke-page.ts"), join(pageDir, "main.ts"));
    const rootfsPath = await buildVfs(root, programPath, imagePath, guestProgram);
    const kernelWasmPath = await resolveKernelWasm(root);

    const viteLog: string[] = [];
    vite = spawnFormulaVite({
      kandeloRoot: root,
      pageRoot: pageDir,
      configPath: join(supportDir, "kms-vite.config.ts"),
      port,
      cwd: browserDemoDir,
      environment: {
        ...process.env,
        KANDELO_FORMULA_BROWSER_ROOT: root,
        KANDELO_FORMULA_BROWSER_PAGE_ROOT: pageDir,
        KANDELO_FORMULA_BROWSER_KERNEL_WASM: kernelWasmPath,
        KANDELO_FORMULA_BROWSER_ROOTFS_VFS: rootfsPath,
      },
    });
    vite.stdout?.on("data", (data: Buffer) => viteLog.push(data.toString()));
    vite.stderr?.on("data", (data: Buffer) => viteLog.push(data.toString()));
    await waitForVite(`${urlBase}/`, vite, viteLog);

    configurePlaywrightBrowserPath();
    const requireFromBrowserApp = createRequire(join(browserDemoDir, "package.json"));
    const { chromium } = requireFromBrowserApp("playwright") as typeof import("playwright");
    browser = await chromium.launch({
      channel: process.env.KANDELO_PLAYWRIGHT_CHANNEL || "chromium",
      headless: true,
    });
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
      await page.goto(`${urlBase}/`, {
        waitUntil: "domcontentloaded",
        timeout: 60_000,
      });
      try {
        await page.waitForFunction(
          () => (window as unknown as { __kandeloKmsReady?: boolean }).__kandeloKmsReady === true,
          undefined,
          { timeout: 60_000 },
        );
      } catch (error) {
        throw new Error(
          `KMS browser page did not initialize: ${JSON.stringify({ pageErrors, vite: viteLog.join("").slice(-4_000) })}`,
          { cause: error },
        );
      }

      const canvas = page.locator("#kms");
      const blankScreenshot = await canvas.screenshot();
      let result: KmsSmokeResult;
      try {
        result = await page.evaluate(
          async ({ argv, minPageFlips, timeoutMs, vfsUrl }) =>
            (window as unknown as {
              __runKandeloKmsSmoke: (request: unknown) => Promise<KmsSmokeResult>;
            }).__runKandeloKmsSmoke({ argv, minPageFlips, timeoutMs, vfsUrl }),
          {
            argv: [guestProgram, ...config.argv],
            minPageFlips: config.minPageFlips,
            timeoutMs: config.timeoutMs,
            vfsUrl: `${urlBase}/formula.vfs`,
          },
        );

        const renderedScreenshot = await canvas.screenshot();
        const pixelStats = await readPixelStats(page, renderedScreenshot);
        const shaderErrors = /(?:shader compile|program link) FAILED/.test(result.stderr);
        if (
          result.pageFlips < config.minPageFlips ||
          result.width < 1 ||
          result.height < 1 ||
          blankScreenshot.equals(renderedScreenshot) ||
          pixelStats.nonUniformPixels < 1_000 ||
          pixelStats.lumaRange < 5 ||
          shaderErrors ||
          pageErrors.length > 0
        ) {
          throw new Error(
            `KMS browser evidence missing: ${JSON.stringify({
              ...result,
              ...pixelStats,
              screenshotBytes: renderedScreenshot.byteLength,
              changedFromBlank: !blankScreenshot.equals(renderedScreenshot),
              shaderErrors,
              pageErrors,
            })}`,
          );
        }
        process.stdout.write(
          `kandelo-kms-browser-ok flips=${result.pageFlips} size=${result.width}x${result.height} ` +
          `pixels=${pixelStats.nonUniformPixels} luma-range=${pixelStats.lumaRange} ` +
          `screenshot-bytes=${renderedScreenshot.byteLength}\n`,
        );
      } finally {
        await page.evaluate(() =>
          (window as unknown as { __cleanupKandeloKmsSmoke?: () => Promise<void> })
            .__cleanupKandeloKmsSmoke?.(),
        ).catch(() => {});
      }
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
  console.error(error instanceof Error ? (error.stack ?? error.message) : String(error));
  process.exitCode = 1;
});
