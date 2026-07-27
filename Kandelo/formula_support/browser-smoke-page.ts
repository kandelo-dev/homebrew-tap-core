import { BrowserKernel } from "@kandelo-browser-kernel";
import kernelWasmUrl from "@kernel-wasm?url";

interface BrowserSmokeRequest {
  argv: string[];
  argv0: string;
  env: Record<string, string>;
  timeoutMs: number;
  guestProgram: string;
  vfsUrl: string;
  launchCount: number;
  maxProcessMemoryBytes?: number;
}

interface BrowserSmokeResult {
  exitCode: number;
  stdout: string;
  stderr: string;
  mergedOutput: string;
}

let activeKernel: BrowserKernel | null = null;

async function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(
          () => reject(new Error(`formula browser process exceeded ${timeoutMs} ms`)),
          timeoutMs,
        );
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function waitForProcessMemory(
  kernel: BrowserKernel,
  pid: number,
  timeoutMs: number,
): Promise<number> {
  const deadline = Date.now() + Math.min(timeoutMs, 10_000);
  while (Date.now() < deadline) {
    const process = (await kernel.enumProcs()).find((candidate) => candidate.pid === pid);
    if (process?.memoryBytes != null) return process.memoryBytes;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(`formula browser process ${pid} did not report its memory size`);
}

async function waitForProcessTreeRemoval(kernel: BrowserKernel): Promise<void> {
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    if ((await kernel.enumProcs()).length === 0) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  const remaining = (await kernel.enumProcs()).map((candidate) => candidate.pid);
  throw new Error(
    `formula browser process tree remained after exit: ${remaining.join(",")}`,
  );
}

async function run(request: BrowserSmokeRequest): Promise<BrowserSmokeResult> {
  if (activeKernel) throw new Error("a formula browser process is already running");

  const stdoutDecoder = new TextDecoder();
  const stderrDecoder = new TextDecoder();
  const mergedChunks: Uint8Array[] = [];
  let stdout = "";
  let stderr = "";
  const kernel = new BrowserKernel({
    kernelOwnedFs: true,
    maxWorkers: 6,
    maxMemoryPages: 16_384,
    onStdout: (data) => {
      stdout += stdoutDecoder.decode(data, { stream: true });
      mergedChunks.push(data.slice());
    },
    onStderr: (data) => {
      stderr += stderrDecoder.decode(data, { stream: true });
      mergedChunks.push(data.slice());
    },
  });
  activeKernel = kernel;

  try {
    const [kernelWasm, vfsImage] = await Promise.all([
      fetch(kernelWasmUrl).then((response) => {
        if (!response.ok) throw new Error(`fetch kernel Wasm failed: ${response.status}`);
        return response.arrayBuffer();
      }),
      fetch(request.vfsUrl).then((response) => {
        if (!response.ok) throw new Error(`fetch formula VFS failed: ${response.status}`);
        return response.arrayBuffer().then((bytes) => new Uint8Array(bytes));
      }),
    ]);

    const guestEnv = new Map<string, string>([
      ["HOME", "/root"],
      ["TMPDIR", "/tmp"],
      ["TERM", "xterm-256color"],
      ["LANG", "C.UTF-8"],
      ["USER", "root"],
      ["LOGNAME", "root"],
      ["PATH", "/usr/local/bin:/usr/bin:/bin"],
      ...Object.entries(request.env),
    ]);
    const firstProcess = await kernel.boot({
      kernelWasm,
      vfsImage,
      argv: [request.guestProgram, ...request.argv],
      env: [...guestEnv].map(([key, value]) => `${key}=${value}`),
      cwd: "/root",
      uid: 0,
      gid: 0,
      stdin: new Uint8Array(),
    });
    let exitCode = 0;
    for (let index = 0; index < request.launchCount; index++) {
      const process = index === 0
        ? firstProcess
        : await kernel.spawnFromVfs(
          request.guestProgram,
          [request.guestProgram, ...request.argv],
          {
            env: [...guestEnv].map(([key, value]) => `${key}=${value}`),
            cwd: "/root",
            uid: 0,
            gid: 0,
            stdin: new Uint8Array(),
          },
        );
      const memoryBytes = request.maxProcessMemoryBytes === undefined
        ? null
        : await waitForProcessMemory(kernel, process.pid, request.timeoutMs);
      exitCode = await withTimeout(process.exit, request.timeoutMs);
      // WHY: a parent exit is not sufficient lifecycle evidence. Require every
      // child and zombie from this launch to disappear before reusing the same
      // kernel for the next launch.
      await waitForProcessTreeRemoval(kernel);
      if (
        memoryBytes !== null &&
        memoryBytes >= request.maxProcessMemoryBytes!
      ) {
        throw new Error(
          `formula browser process ${process.pid} used ${memoryBytes} bytes; ` +
          `expected less than ${request.maxProcessMemoryBytes}`,
        );
      }
      if (exitCode !== request.expectedStatus) break;
    }
    stdout += stdoutDecoder.decode();
    stderr += stderrDecoder.decode();
    const mergedBytes = new Uint8Array(
      mergedChunks.reduce((total, chunk) => total + chunk.byteLength, 0),
    );
    let offset = 0;
    for (const chunk of mergedChunks) {
      mergedBytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return {
      exitCode,
      stdout,
      stderr,
      mergedOutput: new TextDecoder().decode(mergedBytes),
    };
  } finally {
    await kernel.destroy().catch(() => {});
    activeKernel = null;
  }
}

Object.assign(window, {
  __kandeloFormulaBrowserReady: true,
  __runKandeloFormulaBrowserSmoke: run,
  __cleanupKandeloFormulaBrowserSmoke: async () => {
    await activeKernel?.destroy().catch(() => {});
    activeKernel = null;
  },
});
