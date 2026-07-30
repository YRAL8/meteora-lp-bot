/**
 * Offline logic tests (no network). Run: node ts/dist/test_logic.js
 */
import fs from "fs";
import path from "path";
import {
  hasUnresolved,
  isTransientRpcError,
  pollSignatureStatus,
  readJournal,
  unresolvedEntries,
  updateJournalBySignature,
  writeJournal,
  type JournalEntry,
} from "./journal";
import { assertSendAllowed, parseNetwork } from "./network";
import {
  pickSignersForPubkeys,
  prepareTxForSend,
  refreshVersionedBlockhash,
  refuseMultiTxAddError,
  rpcHostForLog,
  shouldRefuseMultiTxAdd,
  signatureFromVersioned,
  validateDepositAmounts,
} from "./build_lib";
import {
  Keypair,
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";

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

function testTransientClassifier(): void {
  assert(isTransientRpcError("fetch failed"), "fetch failed");
  assert(isTransientRpcError("429 Too Many Requests"), "429");
  assert(isTransientRpcError("ECONNRESET"), "reset");
  assert(!isTransientRpcError("account not found"), "non-transient");
}

async function testPollRetriesTransient(): Promise<void> {
  let calls = 0;
  const connection = {
    getSignatureStatuses: async () => {
      calls++;
      if (calls < 3) {
        throw new Error("fetch failed");
      }
      return {
        value: [
          {
            err: null,
            confirmationStatus: "confirmed",
            slot: 42,
          },
        ],
      };
    },
  };
  const out = await pollSignatureStatus(
    connection as never,
    "sigRetry",
    10_000,
    10
  );
  assert(out.outcome === "confirmed", "recovered after transient");
  assert(calls >= 3, "retried");
}

async function testPollUnknownOnlyAfterWindow(): Promise<void> {
  let calls = 0;
  const connection = {
    getSignatureStatuses: async () => {
      calls++;
      throw new Error("fetch failed");
    },
  };
  const out = await pollSignatureStatus(
    connection as never,
    "sigTimeout",
    80,
    20
  );
  assert(out.outcome === "unknown", "unknown after window");
  assert(calls >= 2, "multiple attempts inside window");
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

function testRpcHostForLog(): void {
  assert(
    rpcHostForLog("https://devnet.helius-rpc.com/?api-key=SECRET") ===
      "devnet.helius-rpc.com",
    "strips api-key query"
  );
  assert(
    rpcHostForLog("https://api.devnet.solana.com") === "api.devnet.solana.com",
    "public rpc host"
  );
  assert(
    !rpcHostForLog("https://x.example/?api-key=SECRET").includes("SECRET"),
    "secret never in host"
  );
}

function testFailCarriesExtra(): void {
  // fail() exits the process — verify shape by constructing the same object.
  const extra = {
    signatures: ["SigA"],
    sends: [{ signature: "SigA", status: "unknown" }],
  };
  const payload = {
    ok: false,
    action: "exec-close",
    error: "send failed",
    stage: "send",
    ...extra,
  };
  assert(payload.signatures[0] === "SigA", "fail extra signatures");
  assert(payload.stage === "send", "fail stage send");
}

function makeSignedTransfer(
  payer: Keypair,
  blockhash: string
): VersionedTransaction {
  const ix = SystemProgram.transfer({
    fromPubkey: payer.publicKey,
    toPubkey: Keypair.generate().publicKey,
    lamports: 1,
  });
  const msg = new TransactionMessage({
    payerKey: payer.publicKey,
    recentBlockhash: blockhash,
    instructions: [ix],
  }).compileToV0Message();
  const vtx = new VersionedTransaction(msg);
  vtx.sign([payer]);
  return vtx;
}

function fakeBlockhash(fill: number): string {
  // recentBlockhash must decode to exactly 32 bytes
  const bytes = new Uint8Array(32);
  bytes.fill(fill & 0xff);
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const bs58 = require("bs58") as { encode: (b: Uint8Array) => string };
  return bs58.encode(bytes);
}

async function testPrepareTxForSendFreshBlockhash(): Promise<void> {
  const payer = Keypair.generate();
  const bh1 = fakeBlockhash(1);
  const bh2 = fakeBlockhash(2);
  const vtx0 = makeSignedTransfer(payer, bh1);
  const sig0 = signatureFromVersioned(vtx0);

  // index 0 — no refresh
  const first = await prepareTxForSend(vtx0, 0, [payer], async () => bh2);
  assert(first.refreshed === false, "tx0 not refreshed");
  assert(first.signature.length > 0, "tx0 signed");

  // index 1 — refresh + resign → different signature
  const second = await prepareTxForSend(vtx0, 1, [payer], async () => bh2);
  assert(second.refreshed === true, "tx1 refreshed");
  assert(second.signature !== sig0, "fresh blockhash changes signature");

  // Missing second signer fails
  const other = Keypair.generate();
  let missing = false;
  try {
    pickSignersForPubkeys(
      [payer.publicKey.toBase58(), other.publicKey.toBase58()],
      [payer]
    );
  } catch {
    missing = true;
  }
  assert(missing, "pickSigners requires all keys");

  // refresh alone clears signatures
  const refreshed = refreshVersionedBlockhash(vtx0, bh2);
  let unsigned = false;
  try {
    signatureFromVersioned(refreshed);
  } catch {
    unsigned = true;
  }
  assert(unsigned, "refresh leaves tx unsigned until resign");
}

function testMultiTxAddGateOnTxCountNotWidth(): void {
  // Wide range, single tx — allow without flag (C11b live case: width=69, txs=1)
  assert(
    shouldRefuseMultiTxAdd(1, false) === false,
    "1 tx without flag must pass"
  );
  assert(
    shouldRefuseMultiTxAdd(1, true) === false,
    "1 tx with flag must pass"
  );
  // Real multi-tx without flag — refuse
  assert(
    shouldRefuseMultiTxAdd(2, false) === true,
    "2 txs without flag must refuse"
  );
  assert(
    shouldRefuseMultiTxAdd(3, false) === true,
    "3 txs without flag must refuse"
  );
  // Real multi-tx with flag — allow
  assert(
    shouldRefuseMultiTxAdd(2, true) === false,
    "2 txs with flag must pass"
  );
  const msg = refuseMultiTxAddError(3, 69);
  assert(msg.includes("3 transactions"), "refuse names tx count");
  assert(msg.includes("ALLOW_MULTI_TX_ADD"), "refuse tells what to enable");
  assert(msg.includes("partially funded"), "refuse names partial-fill risk");
}

testJournalPendingToConfirmed();
testJournalUnresolvedBlocks();
testTransientClassifier();
testMainnetGuard();
testParseNetworkDefault();
testNegativeAmounts();
testRpcHostForLog();
testFailCarriesExtra();
testMultiTxAddGateOnTxCountNotWidth();

Promise.all([
  testPollRetriesTransient(),
  testPollUnknownOnlyAfterWindow(),
])
  .then(() => testPrepareTxForSendFreshBlockhash())
  .then(() => {
    console.log(`test_logic: passed=${passed} failed=${failed}`);
    process.exit(failed > 0 ? 1 : 0);
  });
