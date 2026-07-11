import { readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

async function main(): Promise<void> {
  const [root, programPath, ...args] = process.argv.slice(2);
  if (!root || !programPath) {
    throw new Error(
      "usage: run-network-wasm.ts KANDELO_ROOT PROGRAM [ARGS...]",
    );
  }

  const moduleUrl = pathToFileURL(
    join(root, "host/src/node-kernel-host.ts"),
  ).href;
  const memoryFsUrl = pathToFileURL(
    join(root, "host/src/vfs/memory-fs.ts"),
  ).href;
  const [{ NodeKernelHost }, { MemoryFileSystem }] = await Promise.all([
    import(moduleUrl),
    import(memoryFsUrl),
  ]);
  const bytes = readFileSync(programPath);
  const program = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  );
  const rootfs = MemoryFileSystem.create(
    new SharedArrayBuffer(2 * 1024 * 1024),
  );
  const rootfsImage = await rootfs.saveImage();
  const host = new NodeKernelHost({
    maxWorkers: 8,
    enableTcpNetwork: true,
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
    const exit = host.spawn(program, [programPath, ...args], {
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
