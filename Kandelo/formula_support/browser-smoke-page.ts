import { BrowserKernel } from "@kandelo-browser-kernel";
import kernelWasmUrl from "@kernel-wasm?url";

interface BrowserSmokeRequest {
  argv: string[];
  argv0: string;
  env: Record<string, string>;
  timeoutMs: number;
  guestProgram: string;
  vfsUrl: string;
}

interface BrowserSmokeResult {
  exitCode: number;
  stdout: string;
  stderr: string;
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

async function run(request: BrowserSmokeRequest): Promise<BrowserSmokeResult> {
  if (activeKernel) throw new Error("a formula browser process is already running");

  const stdoutDecoder = new TextDecoder();
  const stderrDecoder = new TextDecoder();
  let stdout = "";
  let stderr = "";
  const kernel = new BrowserKernel({
    kernelOwnedFs: true,
    maxWorkers: 6,
    maxMemoryPages: 16_384,
    onStdout: (data) => { stdout += stdoutDecoder.decode(data, { stream: true }); },
    onStderr: (data) => { stderr += stderrDecoder.decode(data, { stream: true }); },
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
    const process = await kernel.boot({
      kernelWasm,
      vfsImage,
      argv: [request.guestProgram, ...request.argv],
      env: [...guestEnv].map(([key, value]) => `${key}=${value}`),
      cwd: "/root",
      uid: 0,
      gid: 0,
      stdin: new Uint8Array(),
    });
    const exitCode = await withTimeout(
      process.exit,
      request.timeoutMs,
    );
    stdout += stdoutDecoder.decode();
    stderr += stderrDecoder.decode();
    return { exitCode, stdout, stderr };
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
