import path from "node:path";
import type { Plugin, UserConfig } from "vite";

const root = process.env.KANDELO_FORMULA_BROWSER_ROOT;
const pageRoot = process.env.KANDELO_FORMULA_BROWSER_PAGE_ROOT;
const kernelWasm = process.env.KANDELO_FORMULA_BROWSER_KERNEL_WASM;
const rootfsVfs = process.env.KANDELO_FORMULA_BROWSER_ROOTFS_VFS;

if (!root || !pageRoot || !kernelWasm || !rootfsVfs) {
  throw new Error("formula browser runner environment is incomplete");
}

function artifacts(): Plugin {
  return {
    name: "kandelo-formula-browser-artifacts",
    enforce: "pre",
    resolveId(source) {
      const queryIndex = source.indexOf("?");
      const name = queryIndex === -1 ? source : source.slice(0, queryIndex);
      const query = queryIndex === -1 ? "" : source.slice(queryIndex);
      if (name === "@kernel-wasm") return kernelWasm + query;
      if (name === "@rootfs-vfs") return rootfsVfs + query;
      if (name === "@kandelo-browser-kernel") {
        return path.join(root, "host/src/browser-kernel-host.ts");
      }
      return null;
    },
  };
}

export default {
  root: pageRoot,
  plugins: [artifacts()],
  server: {
    headers: {
      "Cross-Origin-Embedder-Policy": "require-corp",
      "Cross-Origin-Opener-Policy": "same-origin",
    },
    fs: {
      allow: [root, pageRoot, path.dirname(kernelWasm), path.dirname(rootfsVfs)],
    },
    hmr: false,
  },
  worker: { format: "es" },
  assetsInclude: ["**/*.wasm", "**/*.vfs"],
} satisfies UserConfig;
