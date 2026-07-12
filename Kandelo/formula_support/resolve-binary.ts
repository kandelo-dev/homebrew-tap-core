import { isAbsolute, join, normalize } from "node:path";
import { pathToFileURL } from "node:url";

async function main(): Promise<void> {
  const [root, relPath, ...extra] = process.argv.slice(2);
  if (!root || !relPath || extra.length > 0) {
    throw new Error("usage: resolve-binary.ts KANDELO_ROOT REL_PATH");
  }
  if (!isAbsolute(root) || normalize(root) !== root) {
    throw new Error(`Kandelo root must be absolute and normalized: ${root}`);
  }
  if (
    isAbsolute(relPath) ||
    normalize(relPath) !== relPath ||
    relPath === "." ||
    relPath === ".." ||
    relPath.startsWith("../") ||
    relPath.includes("\\") ||
    relPath.includes("\0")
  ) {
    throw new Error(`invalid Kandelo binary resolver path: ${relPath}`);
  }

  const resolverUrl = pathToFileURL(
    join(root, "host/src/binary-resolver.ts"),
  ).href;
  const { resolveBinary } = await import(resolverUrl);
  process.stdout.write(resolveBinary(relPath));
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
