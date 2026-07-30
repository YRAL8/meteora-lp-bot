import fs from "fs";
import path from "path";
import { Connection } from "@solana/web3.js";

export type JournalStatus = "pending" | "confirmed" | "failed" | "unknown";

export type JournalEntry = {
  ts: string;
  action: string;
  network: string;
  params: Record<string, unknown>;
  signature: string;
  status: JournalStatus;
  slot?: number | null;
  error?: string | null;
  txIndex?: number;
};

export const UNRESOLVED_STATUSES: JournalStatus[] = ["pending", "unknown"];

/** Must match Python ``exec_journal_io.JOURNAL_BASENAME`` / ``LOCK_BASENAME``. */
export const JOURNAL_BASENAME = "exec_journal.jsonl";
export const LOCK_BASENAME = "exec_journal.lock";

/** Single door for paths — do not invent other lock/journal basenames. */
export function journalPaths(projectRoot?: string): {
  journalPath: string;
  lockPath: string;
} {
  const envDir = (process.env.METEORA_STATE_DIR || "").trim();
  const dir = envDir
    ? envDir
    : path.join(projectRoot || path.resolve(__dirname, "..", ".."), "state");
  return {
    journalPath: path.join(dir, JOURNAL_BASENAME),
    lockPath: path.join(dir, LOCK_BASENAME),
  };
}

export function defaultJournalPath(projectRoot?: string): string {
  return journalPaths(projectRoot).journalPath;
}

export function journalLockPath(journalPath: string): string {
  // Sibling lock in the same directory — never `${journalPath}.lock`.
  return path.join(path.dirname(journalPath), LOCK_BASENAME);
}

function sleepMs(ms: number): void {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    /* spin — sync lock helper must not yield */
  }
}

function breakStaleLock(lockPath: string): boolean {
  let raw: string;
  try {
    raw = fs.readFileSync(lockPath, "utf8").trim();
  } catch {
    return false;
  }
  const pid = Number.parseInt(raw.split("\n")[0] || "", 10);
  if (!Number.isFinite(pid)) {
    try {
      fs.unlinkSync(lockPath);
      return true;
    } catch {
      return false;
    }
  }
  try {
    process.kill(pid, 0);
    return false; // alive
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code === "ESRCH") {
      try {
        fs.unlinkSync(lockPath);
        return true;
      } catch {
        return false;
      }
    }
    return false;
  }
}

/**
 * Exclusive lock shared with Python ``journal_lock.py`` (O_EXCL lockfile).
 */
export function withFileLockSync<T>(lockPath: string, fn: () => T): T {
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });
  const start = Date.now();
  for (;;) {
    try {
      const fd = fs.openSync(lockPath, "wx");
      try {
        fs.writeFileSync(fd, `${process.pid}\n`, "utf8");
      } finally {
        fs.closeSync(fd);
      }
      break;
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== "EEXIST") throw err;
      if (breakStaleLock(lockPath)) continue;
      if (Date.now() - start > 60_000) {
        throw new Error(`journal lock timeout: ${lockPath}`);
      }
      sleepMs(50);
    }
  }
  try {
    return fn();
  } finally {
    try {
      fs.unlinkSync(lockPath);
    } catch {
      /* ignore */
    }
  }
}

export function readJournal(journalPath: string): JournalEntry[] {
  if (!fs.existsSync(journalPath)) return [];
  const text = fs.readFileSync(journalPath, "utf8").trim();
  if (!text) return [];
  const entries: JournalEntry[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    try {
      entries.push(JSON.parse(line) as JournalEntry);
    } catch {
      // Skip corrupt lines — must not break resolve/forget (C10).
    }
  }
  return entries;
}

function uniqueTmpPath(journalPath: string): string {
  return `${journalPath}.${process.pid}.${Date.now()}.${Math.random()
    .toString(16)
    .slice(2)}.tmp`;
}

export function writeJournal(journalPath: string, entries: JournalEntry[]): void {
  const dir = path.dirname(journalPath);
  fs.mkdirSync(dir, { recursive: true });
  const body = entries.map((e) => JSON.stringify(e)).join("\n");
  const tmp = uniqueTmpPath(journalPath);
  withFileLockSync(journalLockPath(journalPath), () => {
    fs.writeFileSync(tmp, body ? body + "\n" : "", "utf8");
    fs.renameSync(tmp, journalPath);
  });
}

export function appendJournalEntry(
  journalPath: string,
  entry: JournalEntry
): void {
  const dir = path.dirname(journalPath);
  fs.mkdirSync(dir, { recursive: true });
  withFileLockSync(journalLockPath(journalPath), () => {
    fs.appendFileSync(journalPath, JSON.stringify(entry) + "\n", "utf8");
  });
}

export function updateJournalBySignature(
  journalPath: string,
  signature: string,
  patch: Partial<JournalEntry>
): void {
  withFileLockSync(journalLockPath(journalPath), () => {
    const entries = readJournal(journalPath);
    let found = false;
    for (let i = entries.length - 1; i >= 0; i--) {
      if (entries[i].signature === signature) {
        entries[i] = { ...entries[i], ...patch };
        found = true;
        break;
      }
    }
    if (!found) {
      throw new Error(`journal entry not found for signature ${signature}`);
    }
    const dir = path.dirname(journalPath);
    fs.mkdirSync(dir, { recursive: true });
    const body = entries.map((e) => JSON.stringify(e)).join("\n");
    const tmp = uniqueTmpPath(journalPath);
    fs.writeFileSync(tmp, body ? body + "\n" : "", "utf8");
    fs.renameSync(tmp, journalPath);
  });
}

export function latestEntryBySignature(
  entries: JournalEntry[],
  signature: string
): JournalEntry | undefined {
  for (let i = entries.length - 1; i >= 0; i--) {
    if (entries[i].signature === signature) return entries[i];
  }
  return undefined;
}

/** Unresolved = latest row per signature still pending/unknown. */
export function unresolvedEntries(entries: JournalEntry[]): JournalEntry[] {
  const bySig = new Map<string, JournalEntry>();
  for (const e of entries) {
    bySig.set(e.signature, e);
  }
  return [...bySig.values()].filter((e) =>
    UNRESOLVED_STATUSES.includes(e.status)
  );
}

export function hasUnresolved(entries: JournalEntry[]): boolean {
  return unresolvedEntries(entries).length > 0;
}

export type ConfirmOutcome = "confirmed" | "failed" | "unknown";

/** Same transient class as build_lib.withRpcRetry — do not give up the poll window. */
export function isTransientRpcError(msg: string): boolean {
  const low = msg.toLowerCase();
  return (
    msg.includes("429") ||
    low.includes("too many requests") ||
    low.includes("rate limit") ||
    low.includes("fetch failed") ||
    low.includes("socket hang up") ||
    low.includes("network error") ||
    low.includes("econnreset") ||
    low.includes("econnrefused") ||
    low.includes("etimedout") ||
    low.includes("timeout") ||
    low.includes("eai_again") ||
    low.includes("502") ||
    low.includes("503") ||
    low.includes("504")
  );
}

export async function pollSignatureStatus(
  connection: Connection,
  signature: string,
  timeoutMs = 90_000,
  pollMs = 2_000
): Promise<{ outcome: ConfirmOutcome; slot: number | null; error: string | null }> {
  const start = Date.now();
  let lastError: string | null = null;
  while (Date.now() - start < timeoutMs) {
    let resp;
    try {
      resp = await connection.getSignatureStatuses([signature], {
        searchTransactionHistory: true,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      lastError = msg;
      if (isTransientRpcError(msg)) {
        await new Promise((r) => setTimeout(r, pollMs));
        continue;
      }
      await new Promise((r) => setTimeout(r, pollMs));
      continue;
    }
    const st = resp.value[0];
    if (st) {
      if (st.err) {
        return {
          outcome: "failed",
          slot: st.slot ?? null,
          error: JSON.stringify(st.err),
        };
      }
      const conf = st.confirmationStatus;
      if (conf === "confirmed" || conf === "finalized") {
        return { outcome: "confirmed", slot: st.slot ?? null, error: null };
      }
    }
    await new Promise((r) => setTimeout(r, pollMs));
  }
  return { outcome: "unknown", slot: null, error: lastError };
}

export async function resolveJournalOnStartup(
  connection: Connection,
  journalPath: string
): Promise<{ entries: JournalEntry[]; resolved: JournalEntry[] }> {
  const entries = readJournal(journalPath);
  const open = unresolvedEntries(entries);
  const resolved: JournalEntry[] = [];
  for (const e of open) {
    const { outcome, slot, error } = await pollSignatureStatus(
      connection,
      e.signature,
      15_000,
      1_500
    );
    if (outcome === "confirmed") {
      updateJournalBySignature(journalPath, e.signature, {
        status: "confirmed",
        slot,
        error: null,
      });
      resolved.push({ ...e, status: "confirmed", slot, error: null });
    } else if (outcome === "failed") {
      updateJournalBySignature(journalPath, e.signature, {
        status: "failed",
        slot,
        error,
      });
      resolved.push({ ...e, status: "failed", slot, error });
    }
  }
  return { entries: readJournal(journalPath), resolved };
}
