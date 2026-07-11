import { readFileSync, statSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { rootfsSizeForStagedBytes, validateGuestPath } from "./rootfs-size.ts";

const O_WRONLY = 0x0001;
const O_CREAT = 0x0040;
const O_TRUNC = 0x0200;
const S_IFMT = 0xf000;
const S_IFDIR = 0x4000;

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

function writeGuestFile(
  rootfs: WritableRootfs,
  guestPath: string,
  bytes: Uint8Array,
  mode: number,
): void {
  const parts = guestPath.split("/").filter(Boolean);
  let parent = "";
  for (let i = 0; i < parts.length - 1; i++) {
    parent += `/${parts[i]}`;
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

async function main(): Promise<void> {
  const [root, programPath, ...args] = process.argv.slice(2);
  if (!root || !programPath) {
    throw new Error(
      "usage: run-network-wasm.ts KANDELO_ROOT PROGRAM [ARGS...]",
    );
  }

  const execPrograms = JSON.parse(
    process.env.KANDELO_FORMULA_EXEC_PROGRAMS_JSON ?? "{}",
  ) as Record<string, string>;
  const guestFiles = JSON.parse(
    process.env.KANDELO_FORMULA_GUEST_FILES_JSON ?? "{}",
  ) as Record<string, string>;
  const writableHostDirectories = JSON.parse(
    process.env.KANDELO_FORMULA_WRITABLE_HOST_DIRS_JSON ?? "{}",
  ) as Record<string, string>;
  const configuredArgv0 = process.env.KANDELO_FORMULA_ARGV0;
  const argv0 = configuredArgv0 ?? programPath;
  const guestPaths = [...Object.keys(guestFiles), ...Object.keys(execPrograms)];
  for (const guestPath of guestPaths) validateGuestPath(guestPath, []);
  if (configuredArgv0 !== undefined) validateGuestPath(argv0, []);
  for (const guestPath of Object.keys(execPrograms)) {
    if (guestPath in guestFiles) {
      throw new Error(`guest path is both a file and executable: ${guestPath}`);
    }
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
  const overlaidRoots = [
    ...DEFAULT_MOUNT_SPEC.filter(
      (mount: { source: string }) => mount.source !== "image",
    ).map((mount: { path: string }) => mount.path),
    "/dev",
    "/proc",
  ];
  const writableGuestRoots = Object.keys(writableHostDirectories);
  for (let index = 0; index < writableGuestRoots.length; index++) {
    const guestRoot = writableGuestRoots[index];
    validateGuestPath(guestRoot, overlaidRoots);
    for (const otherRoot of writableGuestRoots.slice(index + 1)) {
      if (
        guestRoot.startsWith(`${otherRoot}/`) ||
        otherRoot.startsWith(`${guestRoot}/`)
      ) {
        throw new Error(
          `writable host-backed guest directories must not overlap: ${guestRoot}, ${otherRoot}`,
        );
      }
    }

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
  for (const guestPath of guestPaths) {
    validateGuestPath(guestPath, overlaidRoots);
    const hiddenByWritableMount = writableGuestRoots.find(
      (root) => guestPath === root || guestPath.startsWith(`${root}/`),
    );
    if (hiddenByWritableMount) {
      throw new Error(
        `guest file path is hidden by writable host mount ${hiddenByWritableMount}: ${guestPath}`,
      );
    }
  }

  const bytes = readFileSync(programPath);
  const program = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  );

  const stagedFiles = [
    ...Object.entries(guestFiles).map(([guestPath, hostPath]) => ({
      guestPath,
      bytes: readFileSync(hostPath),
      mode: 0o644,
    })),
    ...Object.entries(execPrograms).map(([guestPath, hostPath]) => ({
      guestPath,
      bytes: readFileSync(hostPath),
      mode: 0o755,
    })),
  ];
  const stagedBytes = stagedFiles.reduce(
    (total, entry) => total + entry.bytes.byteLength,
    0,
  );
  const rootfsSize = rootfsSizeForStagedBytes(stagedBytes);
  const rootfs = MemoryFileSystem.create(new SharedArrayBuffer(rootfsSize));
  for (const entry of stagedFiles) {
    writeGuestFile(rootfs, entry.guestPath, entry.bytes, entry.mode);
  }
  const rootfsImage = await rootfs.saveImage();
  const host = new NodeKernelHost({
    maxWorkers: 8,
    execPrograms,
    extraMounts: Object.entries(writableHostDirectories).map(
      ([mountPoint, hostPath]) => ({ mountPoint, hostPath, readonly: false }),
    ),
    enableTcpNetwork: process.env.KANDELO_FORMULA_ENABLE_NETWORK === "1",
    rootfsImage,
    onStdout: (_pid: number, data: Uint8Array) => process.stdout.write(data),
    onStderr: (_pid: number, data: Uint8Array) => process.stderr.write(data),
  });

  try {
    await host.init();
    const guestEnv = JSON.parse(
      process.env.KANDELO_FORMULA_GUEST_ENV_JSON ?? "{}",
    ) as Record<string, string>;
    const env = Object.entries(guestEnv).map(
      ([key, value]) => `${key}=${value}`,
    );
    if (!("PATH" in guestEnv)) {
      env.push(
        `PATH=${guestEnv.KERNEL_PATH ?? "/usr/local/bin:/usr/bin:/bin"}`,
      );
    }

    const stdin = process.stdin.isTTY
      ? undefined
      : new Uint8Array(await new Response(process.stdin).arrayBuffer());
    const timeoutMs = Number.parseInt(
      guestEnv.TIMEOUT ?? process.env.TIMEOUT ?? "30000",
      10,
    );
    const exit = host.spawn(program, [argv0, ...args], {
      cwd: guestEnv.KERNEL_CWD ?? "/tmp",
      env,
      stdin,
    });
    let timer: ReturnType<typeof setTimeout> | undefined;
    const timeout = new Promise<number>((_resolve, reject) => {
      timer = setTimeout(
        () => reject(new Error(`process timed out after ${timeoutMs}ms`)),
        timeoutMs,
      );
    });
    try {
      process.exitCode = await Promise.race([exit, timeout]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  } finally {
    await host.destroy().catch(() => {});
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
