export interface FramebufferEvidenceSnapshot {
  binds: number;
  writes: number;
  nonBlankPixels: number;
  exited: boolean;
}

interface FramebufferEvidenceWait<T extends FramebufferEvidenceSnapshot> {
  minWrites: number;
  minNonBlankPixels: number;
  deadline: number;
  now: () => number;
  delay: (milliseconds: number) => Promise<void>;
  sample: () => T;
}

export async function waitForFramebufferEvidence<
  T extends FramebufferEvidenceSnapshot,
>(options: FramebufferEvidenceWait<T>): Promise<T> {
  // A framebuffer bind and write can describe only the black startup frames.
  // Keep the Formula's existing deadline authoritative until the rendered
  // pixel predicate is also true.
  let snapshot = options.sample();
  while (
    !snapshot.exited &&
    options.now() < options.deadline &&
    !(
      snapshot.binds >= 1 &&
      snapshot.writes >= options.minWrites &&
      snapshot.nonBlankPixels >= options.minNonBlankPixels
    )
  ) {
    await options.delay(100);
    snapshot = options.sample();
  }
  return snapshot;
}
