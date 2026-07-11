import { dirname, resolve } from "node:path";

function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value)
    throw new Error(`${name} is required for the framebuffer browser test`);
  return value;
}

const kandeloRoot = requiredEnv("KANDELO_FORMULA_BROWSER_ROOT");
const pageRoot = requiredEnv("KANDELO_FORMULA_BROWSER_PAGE_ROOT");
const kernelWasm = requiredEnv("KANDELO_FORMULA_BROWSER_KERNEL_WASM");
const rootfsVfs = requiredEnv("KANDELO_FORMULA_BROWSER_ROOTFS_VFS");

export default {
  root: pageRoot,
  publicDir: resolve(pageRoot, "public"),
  resolve: {
    alias: [
      { find: "@host", replacement: resolve(kandeloRoot, "host/src") },
      { find: /^@kernel-wasm/, replacement: kernelWasm },
      { find: /^@rootfs-vfs/, replacement: rootfsVfs },
    ],
  },
  server: {
    headers: {
      "Cross-Origin-Embedder-Policy": "require-corp",
      "Cross-Origin-Opener-Policy": "same-origin",
    },
    fs: {
      allow: [kandeloRoot, pageRoot, dirname(kernelWasm), dirname(rootfsVfs)],
    },
    hmr: false,
  },
  worker: {
    format: "es",
  },
  assetsInclude: ["**/*.wasm", "**/*.vfs"],
};
