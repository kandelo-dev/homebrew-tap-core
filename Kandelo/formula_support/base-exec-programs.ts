export type BinaryResolver = (relativePath: string) => string;

export function addDefaultBaseExecPrograms(
  execPrograms: Record<string, string>,
  resolveBinary: BinaryResolver,
): Record<string, string> {
  if (!Object.hasOwn(execPrograms, "/bin/sh")) {
    execPrograms["/bin/sh"] = Object.hasOwn(execPrograms, "/bin/dash")
      ? execPrograms["/bin/dash"]
      : resolveBinary("programs/dash.wasm");
  }
  return execPrograms;
}
