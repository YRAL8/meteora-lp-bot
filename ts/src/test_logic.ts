/**
 * Offline logic tests (no network). Run: node ts/dist/test_logic.js
 */
import fs from "fs";
import path from "path";
import {
  hasUnresolved,
  readJournal,
  unresolvedEntries,
  updateJournalBySignature,
  writeJournal,
  type JournalEntry,
} from "./journal";
import { assertSendAllowed, parseNetwork } from "./network";
import { validateDepositAmounts } from "./build_lib";

let passed = 0;
let failed = 0;

function assert(cond: boolean, msg: string): void {
  if (cond) {
    passed++;
  } else {
    failed++;
    console.error("FAIL:", msg);
  }
}

function testJournalPendingToConfirmed(): void {
  const p = path.join("/tmp", `meteora-journal-test-${Date.now()}.jsonl`);
  const entry: JournalEntry = {
    ts: new Date().toISOString(),
    action: "exec-open",
    network: "devnet",
    params: { sol: 0.01 },
    signature: "sigPending123",
    status: "pending",
    slot: null,
    error: null,
  };
  writeJournal(p, [entry]);
  updateJournalBySignature(p, "sigPending123", {
    status: "confirmed",
    slot: 999,
  });
  const rows = readJournal(p);
  assert(rows.length === 1, "journal length 1");
  assert(rows[0].status === "confirmed", "status confirmed");
  assert(rows[0].slot === 999, "slot set");
  fs.unlinkSync(p);
}

function testJournalUnresolvedBlocks(): void {
  const entries: JournalEntry[] = [
    {
      ts: "t",
      action: "exec-swap",
      network: "devnet",
      params: {},
      signature: "sigA",
      status: "confirmed",
    },
    {
      ts: "t2",
      action: "exec-open",
      network: "devnet",
      params: {},
      signature: "sigB",
      status: "unknown",
    },
  ];
  assert(hasUnresolved(entries), "unknown blocks");
  const open = unresolvedEntries(entries);
  assert(open.length === 1 && open[0].signature === "sigB", "only unknown");
}

function testMainnetGuard(): void {
  const prev = process.env.METEORA_ALLOW_MAINNET;
  delete process.env.METEORA_ALLOW_MAINNET;
  let blocked = false;
  try {
    assertSendAllowed("mainnet", true, "test");
  } catch {
    blocked = true;
  }
  assert(blocked, "mainnet without env blocked");
  process.env.METEORA_ALLOW_MAINNET = "1";
  let allowed = true;
  try {
    assertSendAllowed("mainnet", true, "test");
  } catch {
    allowed = false;
  }
  assert(allowed, "mainnet with env allowed");
  try {
    assertSendAllowed("devnet", true, "test");
  } catch {
    allowed = false;
  }
  assert(allowed, "devnet send always ok");
  if (prev === undefined) delete process.env.METEORA_ALLOW_MAINNET;
  else process.env.METEORA_ALLOW_MAINNET = prev;
}

function testParseNetworkDefault(): void {
  const n = parseNetwork({}, "test");
  assert(n === "devnet", "default devnet");
}

function testNegativeAmounts(): void {
  const cases: Array<[number | undefined, number | undefined, boolean]> = [
    [-1, 1, false],
    [1, -0.1, false],
    [0, 0, false],
    [0.01, 1, true],
  ];
  for (const [sol, usdc, ok] of cases) {
    const err = validateDepositAmounts(sol, usdc);
    assert((err === null) === ok, `amounts sol=${sol} usdc=${usdc} expect ok=${ok}`);
  }
}

testJournalPendingToConfirmed();
testJournalUnresolvedBlocks();
testMainnetGuard();
testParseNetworkDefault();
testNegativeAmounts();

console.log(`test_logic: passed=${passed} failed=${failed}`);
process.exit(failed > 0 ? 1 : 0);
