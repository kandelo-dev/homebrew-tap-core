import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";

export function configureFormulaPlaywrightBrowserPath(
  environment: Record<string, string | undefined> = process.env,
): void {
  if (environment.PLAYWRIGHT_BROWSERS_PATH) return;

  const roots = [environment.HOMEBREW_CACHE, environment.TMPDIR];
  for (const root of roots) {
    if (!root) continue;
    const browserCache = resolve(dirname(resolve(root)), "ms-playwright");
    if (!existsSync(browserCache)) continue;
    environment.PLAYWRIGHT_BROWSERS_PATH = browserCache;
    return;
  }
}
