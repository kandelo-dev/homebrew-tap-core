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

export function resolveBaseExecPrograms(
  execPrograms: Record<string, string>,
  resolveBinary: BinaryResolver,
  mode: string,
): Record<string, string> {
  if (mode === "explicit") return execPrograms;
  if (mode === "default") {
    return addDefaultBaseExecPrograms(execPrograms, resolveBinary);
  }
  throw new Error(
    'KANDELO_RUNNER_BUILTINS must be "default" or "explicit"',
  );
}
