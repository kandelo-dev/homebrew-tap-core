import { BrowserKernel } from "@host/browser-kernel-host";
import { ABI_VERSION } from "@host/generated/abi";
import { attachCanvas } from "@host/framebuffer/canvas-renderer";
import { MemoryFileSystem } from "@host/vfs/memory-fs";
import kernelWasmUrl from "@kernel-wasm?url";

interface FramebufferSmokeRequest {
  argv: string[];
  minWrites: number;
  timeoutMs: number;
  vfsUrl: string;
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

declare global {
  interface Window {
    __kandeloFramebufferReady: boolean;
    __runKandeloFramebufferSmoke: (
      request: FramebufferSmokeRequest,
    ) => Promise<FramebufferSmokeResult>;
  }
}

const canvas = document.getElementById("framebuffer") as HTMLCanvasElement;
let kernelBytes: ArrayBuffer | null = null;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function countNonBlankPixels(): number {
  const context = canvas.getContext("2d");
  if (!context || canvas.width === 0 || canvas.height === 0) return 0;

  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
  const red = pixels[0];
  const green = pixels[1];
  const blue = pixels[2];
  let changed = 0;
  for (let index = 0; index < pixels.length; index += 4) {
    if (
      pixels[index] !== red ||
      pixels[index + 1] !== green ||
      pixels[index + 2] !== blue
    ) {
      changed += 1;
    }
  }
  return changed;
}

async function fetchBytes(url: string, label: string): Promise<ArrayBuffer> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(
      `${label} fetch failed: ${response.status} ${response.statusText}`,
    );
  }
  return response.arrayBuffer();
}

async function runFramebufferSmoke(
  request: FramebufferSmokeRequest,
): Promise<FramebufferSmokeResult> {
  if (!kernelBytes) throw new Error("kernel wasm is not loaded");
  if (!Array.isArray(request.argv) || request.argv.length === 0) {
    throw new Error("argv must contain the guest executable path");
  }

  const vfsBytes = new Uint8Array(
    await fetchBytes(request.vfsUrl, "formula VFS"),
  );
  MemoryFileSystem.assertImageKernelAbi(
    vfsBytes,
    ABI_VERSION,
    "formula framebuffer VFS",
  );

  let stdout = "";
  let stderr = "";
  const stdoutDecoder = new TextDecoder();
  const stderrDecoder = new TextDecoder();
  const ptyDecoder = new TextDecoder();
  const kernel = new BrowserKernel({
    kernelOwnedFs: true,
    onStdout: (data) => {
      stdout += stdoutDecoder.decode(data, { stream: true });
    },
    onStderr: (data) => {
      stderr += stderrDecoder.decode(data, { stream: true });
    },
  });

  let binds = 0;
  let writes = 0;
  let writeBytes = 0;
  let width = 0;
  let height = 0;
  let format: string | null = null;
  let detachCanvas: (() => void) | null = null;
  const offChange = kernel.framebuffers.onChange((pid, event) => {
    if (event !== "bind") return;

    binds += 1;
    const binding = kernel.framebuffers.get(pid);
    if (!binding) return;
    width = binding.w;
    height = binding.h;
    format = binding.fmt;
    canvas.width = width;
    canvas.height = height;
    detachCanvas?.();
    detachCanvas = attachCanvas(canvas, kernel.framebuffers, pid, {
      getProcessMemory: (processId) => kernel.getProcessMemory(processId),
    });
  });
  const offWrite = kernel.framebuffers.onWrite((_pid, _offset, bytes) => {
    writes += 1;
    writeBytes += bytes.length;
  });

  let exited = false;
  let exitCode: number | null = null;
  try {
    const { pid, exit } = await kernel.boot({
      kernelWasm: kernelBytes,
      vfsImage: vfsBytes,
      argv: request.argv,
      cwd: "/",
      env: [
        "HOME=/tmp",
        "TMPDIR=/tmp",
        "TERM=xterm-256color",
        "LANG=C.UTF-8",
        "PATH=/usr/local/bin:/usr/bin:/bin",
      ],
      uid: 0,
      gid: 0,
      pty: true,
    });
    kernel.onPtyOutput(pid, (data) => {
      stdout += ptyDecoder.decode(data, { stream: true });
    });
    void exit.then((status) => {
      exited = true;
      exitCode = status;
    });

    const deadline = performance.now() + request.timeoutMs;
    while (performance.now() < deadline) {
      if (binds >= 1 && writes >= request.minWrites) {
        await delay(800);
        break;
      }
      if (exited) break;
      await delay(100);
    }

    const exitedBeforeCleanup = exited;
    const exitCodeBeforeCleanup = exitCode;
    if (!exitedBeforeCleanup) {
      await kernel.terminateProcess(pid, 0);
    }
    stdout += stdoutDecoder.decode();
    stdout += ptyDecoder.decode();
    stderr += stderrDecoder.decode();

    return {
      binds,
      writes,
      writeBytes,
      width,
      height,
      format,
      nonBlankPixels: countNonBlankPixels(),
      exited: exitedBeforeCleanup,
      exitCode: exitCodeBeforeCleanup,
      stdout,
      stderr,
    };
  } finally {
    offChange();
    offWrite();
    detachCanvas?.();
    await kernel.destroy().catch(() => {});
  }
}

async function init(): Promise<void> {
  kernelBytes = await fetchBytes(kernelWasmUrl, "kernel.wasm");
  window.__runKandeloFramebufferSmoke = runFramebufferSmoke;
  window.__kandeloFramebufferReady = true;
}

window.__kandeloFramebufferReady = false;
void init();
