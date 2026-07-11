import { join } from "node:path";
import { pathToFileURL } from "node:url";

interface PairCase {
  name: string;
  transport: "tcp" | "udp";
  serverArgs: string[];
  clientArgs: string[];
  serverStdin: string;
  clientStdin: string;
  expectedServerStdout?: string;
  expectedServerStdoutIncludes?: string[];
  expectedClientStdout?: string;
  expectedClientStdoutIncludes?: string[];
  timeoutMs?: number;
}

interface PairConfig {
  cases: PairCase[];
}

async function main(): Promise<void> {
  const [root, serverProgramPath, clientProgramPath] = process.argv.slice(2);
  if (!root || !serverProgramPath || !clientProgramPath) {
    throw new Error(
      "usage: run-virtual-network-pairs.ts KANDELO_ROOT SERVER CLIENT",
    );
  }

  const config = JSON.parse(
    process.env.KANDELO_FORMULA_VIRTUAL_PAIRS_JSON ?? "{}",
  ) as PairConfig;
  if (!Array.isArray(config.cases) || config.cases.length === 0) {
    throw new Error("KANDELO_FORMULA_VIRTUAL_PAIRS_JSON must contain cases");
  }

  const moduleUrl = (path: string) => pathToFileURL(join(root, path)).href;
  const [
    { LocalVirtualNetwork },
    { NodePlatformIO },
    { runCentralizedProgram },
  ] = await Promise.all([
    import(moduleUrl("host/src/networking/virtual-network.ts")),
    import(moduleUrl("host/src/platform/node.ts")),
    import(moduleUrl("host/test/centralized-test-helper.ts")),
  ]);

  const summaries: Record<string, unknown> = {};
  for (const pair of config.cases) {
    const network = new LocalVirtualNetwork();
    const serverIO = new NodePlatformIO();
    const clientIO = new NodePlatformIO();
    serverIO.network = network.attachMachine({
      id: `${pair.name}-server`,
      address: [10, 88, 0, 2],
      hostnames: [`${pair.name}-server`],
    });
    clientIO.network = network.attachMachine({
      id: `${pair.name}-client`,
      address: [10, 88, 0, 3],
      hostnames: [`${pair.name}-client`],
    });

    let resolveServerReady!: () => void;
    const serverReady = new Promise<void>((resolve) => {
      resolveServerReady = resolve;
    });
    if (pair.transport === "tcp") {
      const listenTcp = serverIO.network.listenTcp?.bind(serverIO.network);
      if (!listenTcp)
        throw new Error("virtual network has no TCP listener support");
      serverIO.network.listenTcp = (listenerId, addr, port, target) => {
        const status = listenTcp(listenerId, addr, port, target);
        if (status === 0) resolveServerReady();
        return status;
      };
    } else if (pair.transport === "udp") {
      const bindUdp = serverIO.network.bindUdp?.bind(serverIO.network);
      if (!bindUdp) throw new Error("virtual network has no UDP bind support");
      serverIO.network.bindUdp = (endpointId, addr, port, target) => {
        const status = bindUdp(endpointId, addr, port, target);
        if (status === 0) resolveServerReady();
        return status;
      };
    } else {
      throw new Error(
        `${pair.name} has unsupported transport ${String(pair.transport)}`,
      );
    }

    const timeout = pair.timeoutMs ?? 10_000;
    const serverRun = runCentralizedProgram({
      programPath: serverProgramPath,
      argv: pair.serverArgs,
      io: serverIO,
      stdin: pair.serverStdin,
      timeout,
    });
    await Promise.race([
      serverReady,
      serverRun.then((result) => {
        throw new Error(
          `${pair.name} server exited before ${pair.transport} readiness: ` +
            JSON.stringify({
              status: result.exitCode,
              stdout: result.stdout,
              stderr: result.stderr,
            }),
        );
      }),
    ]);
    const clientRun = runCentralizedProgram({
      programPath: clientProgramPath,
      argv: pair.clientArgs,
      io: clientIO,
      stdin: pair.clientStdin,
      timeout,
    });
    const [server, client] = await Promise.all([serverRun, clientRun]);
    const summary = {
      serverStatus: server.exitCode,
      clientStatus: client.exitCode,
      serverStdout: server.stdout,
      serverStderr: server.stderr,
      clientStdout: client.stdout,
      clientStderr: client.stderr,
    };
    const expectedServerStdout = pair.expectedServerStdout;
    const expectedServerIncludes = pair.expectedServerStdoutIncludes ?? [];
    const expectedClientStdout = pair.expectedClientStdout;
    const expectedClientIncludes = pair.expectedClientStdoutIncludes ?? [];
    if (
      server.exitCode !== 0 ||
      client.exitCode !== 0 ||
      (expectedServerStdout !== undefined &&
        server.stdout !== expectedServerStdout) ||
      expectedServerIncludes.some((value) => !server.stdout.includes(value)) ||
      server.stderr !== "" ||
      client.stderr !== "" ||
      (expectedClientStdout !== undefined &&
        client.stdout !== expectedClientStdout) ||
      expectedClientIncludes.some((value) => !client.stdout.includes(value))
    ) {
      throw new Error(`${pair.name} failed: ${JSON.stringify(summary)}`);
    }
    summaries[pair.name] = summary;
  }

  process.stdout.write(`${JSON.stringify(summaries)}\n`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
