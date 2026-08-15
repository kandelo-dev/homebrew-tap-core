import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { describe, expect, it } from "vitest";

type BrowserPathModule = {
  configureFormulaPlaywrightBrowserPath: (
    environment: Record<string, string | undefined>,
  ) => void;
};

async function loadBrowserPathModule(): Promise<BrowserPathModule | null> {
  const modulePath = resolve(
    dirname(fileURLToPath(import.meta.url)),
    "../playwright-browser-path.ts",
  );
  return import(pathToFileURL(modulePath).href)
    .then((loaded) => loaded as BrowserPathModule)
    .catch(() => null);
}

describe("Formula Playwright browser cache", () => {
  it("recovers the provisioned browser from Homebrew's preserved TMPDIR", async () => {
    const browserPathModule = await loadBrowserPathModule();
    expect(browserPathModule).not.toBeNull();
    if (!browserPathModule) return;

    const sharedRoot = mkdtempSync(join(tmpdir(), "kandelo-playwright-path-"));
    try {
      const homebrewTemp = join(sharedRoot, "tmp");
      const browserCache = join(sharedRoot, "ms-playwright");
      mkdirSync(homebrewTemp);
      mkdirSync(browserCache);
      const environment: Record<string, string | undefined> = {
        TMPDIR: homebrewTemp,
      };

      browserPathModule.configureFormulaPlaywrightBrowserPath(environment);

      expect(environment.PLAYWRIGHT_BROWSERS_PATH).toBe(browserCache);
    } finally {
      rmSync(sharedRoot, { recursive: true, force: true });
    }
  });
});
