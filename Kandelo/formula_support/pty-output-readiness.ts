export interface PtyOutputReadiness {
  observe(data: Uint8Array): void;
  wait(): Promise<void>;
}

export function createPtyOutputReadiness(
  readyText: string,
): PtyOutputReadiness {
  if (readyText.length === 0) {
    throw new Error("PTY input readiness text must not be empty");
  }

  const decoder = new TextDecoder();
  let tail = "";
  let matched = false;
  let resolveMatch: (() => void) | undefined;
  let matchPromise: Promise<void> | undefined;

  return {
    observe(data: Uint8Array): void {
      if (matched) return;

      const combined = tail + decoder.decode(data, { stream: true });
      if (combined.includes(readyText)) {
        matched = true;
        tail = "";
        resolveMatch?.();
        return;
      }

      const retainedCharacters = readyText.length - 1;
      tail =
        retainedCharacters === 0
          ? ""
          : combined.slice(-retainedCharacters);
    },
    wait(): Promise<void> {
      if (matched) return Promise.resolve();

      matchPromise ??= new Promise<void>((resolve) => {
        resolveMatch = resolve;
      });
      return matchPromise;
    },
  };
}
