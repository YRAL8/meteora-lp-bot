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

export function defaultJournalPath(projectRoot?: string): string {
  const root =
    projectRoot || path.resolve(__dirname, "..", "..");
  return path.join(root, "state", "exec_journal.jsonl");
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

export function writeJournal(journalPath: string, entries: JournalEntry[]): void {
  const dir = path.dirname(journalPath);
  fs.mkdirSync(dir, { recursive: true });
  const body = entries.map((e) => JSON.stringify(e)).join("\n");
  const tmp = `${journalPath}.tmp`;
  fs.writeFileSync(tmp, body ? body + "\n" : "", "utf8");
  fs.renameSync(tmp, journalPath);
}

export function appendJournalEntry(
  journalPath: string,
  entry: JournalEntry
): void {
  const dir = path.dirname(journalPath);
  fs.mkdirSync(dir, { recursive: true });
  fs.appendFileSync(journalPath, JSON.stringify(entry) + "\n", "utf8");
}

export function updateJournalBySignature(
  journalPath: string,
  signature: string,
  patch: Partial<JournalEntry>
): void {
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
  writeJournal(journalPath, entries);
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
      // Temporary network blip: keep polling until the window is exhausted.
      if (isTransientRpcError(msg)) {
        await new Promise((r) => setTimeout(r, pollMs));
        continue;
      }
      // Non-transient RPC error — still wait out the window once; do not
      // treat a single hiccup as definitive "unknown" if time remains.
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
    // unknown stays unknown — do not auto-retry send
  }
  return { entries: readJournal(journalPath), resolved };
}
