import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import {
  createForkDescendantTracker,
  parseExpectedForkDescendants,
  type ProcessEvent,
} from "./fork-descendant-statuses.ts";
import {
  createPtyOutputReadiness,
  type PtyOutputReadiness,
} from "./pty-output-readiness.ts";
import {
  createPtyCompletionOutputTracker,
  validatePtyCompletionOutput,
  waitForPtyCompletion,
  type PtyCompletionOutputTracker,
} from "./pty-completion-output.ts";
import { rootfsSizeForStagedBytes, validateGuestPath } from "./rootfs-size.ts";

const O_WRONLY = 0x0001;
const O_CREAT = 0x0040;
const O_TRUNC = 0x0200;
const S_IFMT = 0xf000;
const S_IFDIR = 0x4000;
const MAX_PTY_CONFIG_BYTES = 16 * 1024 * 1024;
const MAX_INPUT_READY_TEXT_BYTES = 4 * 1024;

interface PtyConfig {
  argv0?: string | null;
  env: Record<string, string>;
  inputs: string[];
  inputReadyText?: string | null;
  rerunInputs?: string[] | null;
  execPrograms?: Record<string, string>;
  guestFiles?: Record<string, string>;
  guestDirectories?: string[];
  writableGuestDirectories?: string[];
  writableHostDirectories?: Record<string, string>;
  initialDelayMs: number;
  inputDelayMs: number;
  cols: number;
  rows: number;
  timeoutMs?: number | null;
  completionOutput?: string | null;
  expectedForkDescendants?: number;
}

interface WritableRootfs {
  mkdir(path: string, mode: number): void;
  stat(path: string): { mode: number };
  open(path: string, flags: number, mode: number): number;
  write(
    fd: number,
    data: Uint8Array,
    offset: number | null,
    length: number,
  ): number;
  close(fd: number): void;
}

const delay = (milliseconds: number) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

function createGuestDirectory(rootfs: WritableRootfs, guestPath: string): void {
  const parts = guestPath.split("/").filter(Boolean);
  let current = "";
  for (const part of parts) {
    current += `/${part}`;
    try {
      rootfs.mkdir(current, 0o755);
    } catch (error) {
      if ((rootfs.stat(current).mode & S_IFMT) !== S_IFDIR) throw error;
    }
  }
}

function writeGuestFile(
  rootfs: WritableRootfs,
  guestPath: string,
  bytes: Uint8Array,
  mode: number,
): void {
  const parts = guestPath.split("/").filter(Boolean);
  createGuestDirectory(rootfs, `/${parts.slice(0, -1).join("/")}`);

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

function pathWithin(guestPath: string, guestRoot: string): boolean {
  return guestPath === guestRoot || guestPath.startsWith(`${guestRoot}/`);
}

function writableRootFor(
  guestPath: string,
  writableRoots: readonly string[],
): string | undefined {
  return writableRoots.find((guestRoot) => pathWithin(guestPath, guestRoot));
}

async function main(): Promise<void> {
  const [root, programPath, ...args] = process.argv.slice(2);
  if (!root || !programPath) {
    throw new Error("usage: run-pty-wasm.ts KANDELO_ROOT PROGRAM [ARGS...]");
  }

  const configPath = process.env.KANDELO_FORMULA_PTY_CONFIG_PATH;
  if (!configPath) {
    throw new Error("KANDELO_FORMULA_PTY_CONFIG_PATH is required");
  }
  let configStat: ReturnType<typeof statSync>;
  try {
    configStat = statSync(configPath);
  } catch (error) {
    throw new Error(`PTY config file is unavailable: ${String(error)}`);
  }
  if (!configStat.isFile() || configStat.size <= 0 || configStat.size > MAX_PTY_CONFIG_BYTES) {
    throw new Error(
      `PTY config file must be a nonempty regular file no larger than ${MAX_PTY_CONFIG_BYTES} bytes`,
    );
  }
  let config: PtyConfig;
  try {
    config = JSON.parse(readFileSync(configPath, "utf8")) as PtyConfig;
  } catch (error) {
    throw new Error(`PTY config JSON is malformed: ${String(error)}`);
  }
  if (!Array.isArray(config.inputs)) {
    throw new Error("PTY config JSON must contain an inputs array");
  }
  if (
    config.inputReadyText != null &&
    (typeof config.inputReadyText !== "string" ||
      config.inputReadyText.length === 0 ||
      new TextEncoder().encode(config.inputReadyText).byteLength >
        MAX_INPUT_READY_TEXT_BYTES)
  ) {
    throw new Error(
      `inputReadyText must be a nonempty string no larger than ${MAX_INPUT_READY_TEXT_BYTES} bytes`,
    );
  }
  if (config.rerunInputs != null && !Array.isArray(config.rerunInputs)) {
    throw new Error("rerunInputs must be an array when present");
  }
  if (
    config.timeoutMs != null &&
    (!Number.isSafeInteger(config.timeoutMs) || config.timeoutMs <= 0)
  ) {
    throw new Error("timeoutMs must be a positive integer");
  }
  const completionOutput = validatePtyCompletionOutput(
    config.completionOutput,
  );
  const expectedForkDescendants = parseExpectedForkDescendants(
    String(config.expectedForkDescendants ?? 0),
    undefined,
  );
  const inputReadyText = config.inputReadyText ?? undefined;

  const configuredArgv0 = config.argv0 ?? undefined;
  if (configuredArgv0 !== undefined) validateGuestPath(configuredArgv0, []);
  const argv0 = configuredArgv0 ?? programPath;

  const execPrograms = config.execPrograms ?? {};
  const guestFiles = config.guestFiles ?? {};
  const guestDirectories = config.guestDirectories ?? [];
  const writableGuestDirectories = config.writableGuestDirectories ?? [];
  const writableHostDirectories = config.writableHostDirectories ?? {};
  if (!Array.isArray(guestDirectories)) {
    throw new Error("guestDirectories must be an array");
  }
  if (!Array.isArray(writableGuestDirectories)) {
    throw new Error("writableGuestDirectories must be an array");
  }
  if (
    writableHostDirectories === null ||
    typeof writableHostDirectories !== "object" ||
    Array.isArray(writableHostDirectories)
  ) {
    throw new Error("writableHostDirectories must be an object");
  }

  const moduleUrl = pathToFileURL(
    join(root, "host/src/node-kernel-host.ts"),
  ).href;
  const memoryFsUrl = pathToFileURL(
    join(root, "host/src/vfs/memory-fs.ts"),
  ).href;
  const defaultMountsUrl = pathToFileURL(
    join(root, "host/src/vfs/default-mounts.ts"),
  ).href;
  const [{ NodeKernelHost }, { MemoryFileSystem }, { DEFAULT_MOUNT_SPEC }] =
    await Promise.all([
      import(moduleUrl),
      import(memoryFsUrl),
      import(defaultMountsUrl),
    ]);

  const guestPaths = [
    ...Object.keys(execPrograms),
    ...Object.keys(guestFiles),
    ...guestDirectories,
    ...writableGuestDirectories,
    ...Object.keys(writableHostDirectories),
  ];
  for (const guestPath of guestPaths) validateGuestPath(guestPath, []);
  const writableHostRoots = Object.keys(writableHostDirectories);
  const writableRoots = [...writableGuestDirectories, ...writableHostRoots];
  for (let i = 0; i < writableRoots.length; i++) {
    const guestRoot = writableRoots[i];
    if (guestRoot === "/dev" || guestRoot.startsWith("/dev/")) {
      throw new Error(`writable guest mount overlaps /dev: ${guestRoot}`);
    }
    if (guestRoot === "/proc" || guestRoot.startsWith("/proc/")) {
      throw new Error(`writable guest mount overlaps /proc: ${guestRoot}`);
    }
    for (const otherRoot of writableRoots.slice(i + 1)) {
      if (
        pathWithin(guestRoot, otherRoot) ||
        pathWithin(otherRoot, guestRoot)
      ) {
        throw new Error(
          `writable guest mounts must not overlap: ${guestRoot}, ${otherRoot}`,
        );
      }
    }
  }
  const overlaidRoots = [
    ...DEFAULT_MOUNT_SPEC.filter(
      (mount: { source: string }) => mount.source !== "image",
    ).map((mount: { path: string }) => mount.path),
    "/dev",
    "/proc",
  ];
  for (const guestRoot of writableHostRoots) {
    validateGuestPath(guestRoot, overlaidRoots);
    const hostPath = writableHostDirectories[guestRoot];
    if (!isAbsolute(hostPath) || resolve(hostPath) !== hostPath) {
      throw new Error(
        `writable host directory must be absolute and normalized: ${hostPath}`,
      );
    }
    if (!statSync(hostPath).isDirectory()) {
      throw new Error(`writable host path is not a directory: ${hostPath}`);
    }
  }
  for (const guestPath of [
    ...Object.keys(execPrograms),
    ...Object.keys(guestFiles),
    ...guestDirectories,
  ]) {
    const hiddenByHostMount = writableHostRoots.find((guestRoot) =>
      pathWithin(guestPath, guestRoot),
    );
    if (hiddenByHostMount) {
      throw new Error(
        `guest path is hidden by writable host mount ${hiddenByHostMount}: ${guestPath}`,
      );
    }
    if (!writableRootFor(guestPath, writableGuestDirectories)) {
      validateGuestPath(guestPath, overlaidRoots);
    }
  }
  for (const guestRoot of writableRoots) {
    if (overlaidRoots.includes(guestRoot)) {
      throw new Error(
        `writable guest mount conflicts with a runtime mount: ${guestRoot}`,
      );
    }
    if (guestRoot in guestFiles) {
      throw new Error(`guest path is both a file and directory: ${guestRoot}`);
    }
    if (guestRoot in execPrograms) {
      throw new Error(
        `guest path is both an executable and directory: ${guestRoot}`,
      );
    }
  }
  for (const guestPath of Object.keys(execPrograms)) {
    if (guestPath in guestFiles) {
      throw new Error(`guest path is both a file and executable: ${guestPath}`);
    }
  }
  for (const guestDirectory of guestDirectories) {
    if (guestDirectory in guestFiles) {
      throw new Error(
        `guest path is both a file and directory: ${guestDirectory}`,
      );
    }
    if (guestDirectory in execPrograms) {
      throw new Error(
        `guest path is both an executable and directory: ${guestDirectory}`,
      );
    }
  }

  const bytes = readFileSync(programPath);
  const program = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  );
  const guestEnv = config.env ?? {};
  const env = Object.entries(guestEnv).map(([key, value]) => `${key}=${value}`);
  if (!("PATH" in guestEnv)) {
    env.push(`PATH=${guestEnv.KERNEL_PATH ?? "/usr/local/bin:/usr/bin:/bin"}`);
  }

  let writableHostRoot: string | undefined;
  try {
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
      .filter(
        ({ guestPath }) =>
          !writableRootFor(guestPath, writableGuestDirectories),
      )
      .map(({ guestPath, hostPath, mode }) => ({
        guestPath,
        bytes: readFileSync(hostPath),
        mode,
      }));
    const stagedDirectories = guestDirectories.filter(
      (guestPath) => !writableRootFor(guestPath, writableGuestDirectories),
    );
    let rootfsImage: Uint8Array | undefined;
    if (guestPaths.length > 0) {
      const stagedBytes = stagedFiles.reduce(
        (total, entry) => total + entry.bytes.byteLength,
        0,
      );
      const rootfs = MemoryFileSystem.create(
        new SharedArrayBuffer(rootfsSizeForStagedBytes(stagedBytes)),
      );
      for (const guestDirectory of stagedDirectories) {
        createGuestDirectory(rootfs, guestDirectory);
      }
      for (const entry of stagedFiles) {
        writeGuestFile(rootfs, entry.guestPath, entry.bytes, entry.mode);
      }
      rootfsImage = await rootfs.saveImage();
    }

    const extraMounts: Array<{
      mountPoint: string;
      hostPath: string;
      readonly: boolean;
    }> = Object.entries(writableHostDirectories).map(
      ([mountPoint, hostPath]) => ({ mountPoint, hostPath, readonly: false }),
    );
    if (writableGuestDirectories.length > 0) {
      // Keep mutable test state off the readonly root image. A single host
      // instance and mount set is reused by both spawns, matching session state.
      writableHostRoot = mkdtempSync(join(tmpdir(), "kandelo-formula-pty-"));
      for (const [index, guestRoot] of writableGuestDirectories.entries()) {
        const hostRoot = join(writableHostRoot, `mount-${index}`);
        mkdirSync(hostRoot, { recursive: true, mode: 0o755 });
        extraMounts.push({
          mountPoint: guestRoot,
          hostPath: hostRoot,
          readonly: false,
        });

        for (const guestDirectory of guestDirectories) {
          if (!pathWithin(guestDirectory, guestRoot)) continue;

          const relativePath = guestDirectory
            .slice(guestRoot.length)
            .replace(/^\/+/, "");
          if (relativePath) {
            mkdirSync(join(hostRoot, relativePath), {
              recursive: true,
              mode: 0o755,
            });
          }
        }
        const mountedFiles = [
          ...Object.entries(guestFiles).map(([guestPath, sourcePath]) => ({
            guestPath,
            sourcePath,
            mode: 0o644,
          })),
          ...Object.entries(execPrograms).map(([guestPath, sourcePath]) => ({
            guestPath,
            sourcePath,
            mode: 0o755,
          })),
        ];
        for (const { guestPath, sourcePath, mode } of mountedFiles) {
          if (!pathWithin(guestPath, guestRoot)) continue;

          const relativePath = guestPath
            .slice(guestRoot.length)
            .replace(/^\/+/, "");
          const destination = join(hostRoot, relativePath);
          mkdirSync(dirname(destination), { recursive: true, mode: 0o755 });
          writeFileSync(destination, readFileSync(sourcePath), { mode });
        }
      }
    }

    let forkDescendants = createForkDescendantTracker();
    let activeOutputReadiness: PtyOutputReadiness | undefined;
    let completionTracker: PtyCompletionOutputTracker | undefined;
    const observeOutput = (
      destination: NodeJS.WriteStream,
      data: Uint8Array,
    ): void => {
      destination.write(data);
      completionTracker?.observe(data);
    };
    const host = new NodeKernelHost({
      maxWorkers: 4,
      execPrograms,
      rootfsImage,
      extraMounts,
      onPtyOutput: (_pid: number, data: Uint8Array) => {
        activeOutputReadiness?.observe(data);
        observeOutput(process.stdout, data);
      },
      onStderr: (_pid: number, data: Uint8Array) =>
        observeOutput(process.stderr, data),
      onProcessEvent: (event: ProcessEvent) =>
        forkDescendants.onProcessEvent(event),
    });

    try {
      await host.init();
      const timeoutMs =
        config.timeoutMs ??
        Number.parseInt(guestEnv.TIMEOUT ?? process.env.TIMEOUT ?? "30000", 10);
      const run = async (inputs: string[]): Promise<number> => {
        forkDescendants = createForkDescendantTracker();
        completionTracker = completionOutput
          ? createPtyCompletionOutputTracker(completionOutput)
          : undefined;
        const deadline = Date.now() + timeoutMs;
        activeOutputReadiness = inputReadyText
          ? createPtyOutputReadiness(inputReadyText)
          : undefined;
        let timer: ReturnType<typeof setTimeout> | undefined;
        const timeout = new Promise<number>((_resolve, reject) => {
          timer = setTimeout(
            () => reject(new Error(`process timed out after ${timeoutMs}ms`)),
            timeoutMs,
          );
        });
        const exit = host.spawn(program, [argv0, ...args], {
          cwd: guestEnv.KERNEL_CWD ?? (rootfsImage ? "/" : process.cwd()),
          env,
          pty: true,
          ptyCols: config.cols ?? 100,
          ptyRows: config.rows ?? 30,
          onStarted: async (pid: number) => {
            if (activeOutputReadiness) {
              await Promise.race([activeOutputReadiness.wait(), timeout]);
            } else {
              await delay(config.initialDelayMs ?? 500);
            }
            for (const input of inputs) {
              host.ptyWrite(pid, new TextEncoder().encode(input));
              await delay(config.inputDelayMs ?? 180);
            }
          },
        });
        try {
          const status = await waitForPtyCompletion(
            exit,
            timeout,
            completionTracker,
          );
          if (status === 0 && expectedForkDescendants.count > 0) {
            await Promise.race([
              forkDescendants.waitFor(expectedForkDescendants, deadline),
              timeout,
            ]);
          }
          return status;
        } finally {
          completionTracker = undefined;
          if (timer) clearTimeout(timer);
          activeOutputReadiness = undefined;
        }
      };

      const firstStatus = await run(config.inputs);
      process.exitCode = firstStatus;
      if (firstStatus === 0 && config.rerunInputs) {
        process.exitCode = await run(config.rerunInputs);
      }
    } finally {
      await host.destroy().catch(() => {});
    }
  } finally {
    if (writableHostRoot) {
      rmSync(writableHostRoot, { recursive: true, force: true });
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
