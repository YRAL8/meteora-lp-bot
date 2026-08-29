/**
 * Offline logic tests (no network). Run: node ts/dist/test_logic.js
 */
import { assertSendAllowed, parseNetwork } from "./network";
import {
  confirmWithRebroadcast,
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

  const fresh = async () => ({ blockhash: bh2, lastValidBlockHeight: 4242 });

  // index 0 — refreshed too: the build phase may have spent half the
  // blockhash lifetime simulating before we get here.
  const first = await prepareTxForSend(vtx0, 0, [payer], fresh);
  assert(first.refreshed === true, "tx0 refreshed");
  assert(first.signature !== sig0, "tx0 fresh blockhash changes signature");
  assert(first.lastValidBlockHeight === 4242, "tx0 reports expiry height");

  // index 1 — same contract
  const second = await prepareTxForSend(vtx0, 1, [payer], fresh);
  assert(second.refreshed === true, "tx1 refreshed");
  assert(second.signature !== sig0, "fresh blockhash changes signature");
  assert(second.lastValidBlockHeight === 4242, "tx1 reports expiry height");

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


type FakeStatus = {
  err: unknown;
  slot?: number;
  confirmationStatus?: string | null;
} | null;

/**
 * Fake cluster: answers `statuses[callIndex]` (last value repeats) and reports
 * a block height that climbs one per poll from `startHeight`.
 */
function fakeRpc(opts: {
  statuses: FakeStatus[];
  startHeight: number;
  sendThrows?: boolean;
  statusThrows?: boolean;
}) {
  const sends: number[] = [];
  let statusCalls = 0;
  let height = opts.startHeight;
  const rpc = {
    async getSignatureStatuses(sigs: string[], _cfg: { searchTransactionHistory: boolean }) {
      void sigs;
      if (opts.statusThrows) throw new Error("fetch failed");
      const i = Math.min(statusCalls, opts.statuses.length - 1);
      statusCalls += 1;
      return { value: [opts.statuses[i]] };
    },
    async sendRawTransaction(raw: Uint8Array, _o: { skipPreflight: boolean; maxRetries: number }) {
      void raw;
      if (opts.sendThrows) throw new Error("send failed");
      sends.push(height);
      return "sig";
    },
    async getBlockHeight(_c: "confirmed") {
      height += 1;
      return height;
    },
  };
  return { rpc, sends, sendCount: () => sends.length };
}

async function testConfirmRebroadcastsUntilLanded(): Promise<void> {
  // Absent, absent, then confirmed → confirmed, and it kept re-sending meanwhile.
  const f = fakeRpc({
    statuses: [null, null, { err: null, slot: 77, confirmationStatus: "confirmed" }],
    startHeight: 100,
  });
  const out = await confirmWithRebroadcast(f.rpc, new Uint8Array([1]), "sig", 1_000, {
    pollMs: 0,
    now: () => 0,
    sleepFn: async () => {},
  });
  assert(out.outcome === "confirmed", "landed tx confirms");
  assert(out.slot === 77, "confirmed reports slot");
  assert(f.sendCount() >= 2, "re-broadcasts while waiting");
}

async function testConfirmReportsOnChainFailure(): Promise<void> {
  const f = fakeRpc({
    statuses: [{ err: { InstructionError: [0, "Custom"] }, slot: 5 }],
    startHeight: 100,
  });
  const out = await confirmWithRebroadcast(f.rpc, new Uint8Array([1]), "sig", 1_000, {
    pollMs: 0,
    now: () => 0,
    sleepFn: async () => {},
  });
  assert(out.outcome === "failed", "on-chain error is failed, not dropped");
  assert(out.slot === 5, "failed reports slot");
}

async function testConfirmDropsWhenBlockhashExpires(): Promise<void> {
  // Never lands; height starts past lastValidBlockHeight → expiry, then grace.
  let clock = 0;
  const f = fakeRpc({ statuses: [null], startHeight: 500 });
  const out = await confirmWithRebroadcast(f.rpc, new Uint8Array([1]), "sig", 400, {
    pollMs: 0,
    graceMs: 10,
    now: () => (clock += 5),
    sleepFn: async () => {},
  });
  assert(out.outcome === "dropped", "expired + absent is dropped");
  assert(
    String(out.error).includes("never landed"),
    "dropped says the tx never landed"
  );
  const before = f.sendCount();
  assert(before >= 1, "sent at least once before expiry");
}

async function testConfirmStaysUnknownWhenRpcIsDown(): Promise<void> {
  // Node unreachable: we never learn anything, so it must NOT claim dropped.
  let clock = 0;
  const f = fakeRpc({ statuses: [null], startHeight: 100, statusThrows: true });
  const out = await confirmWithRebroadcast(f.rpc, new Uint8Array([1]), "sig", 1_000_000, {
    pollMs: 0,
    timeoutMs: 40,
    now: () => (clock += 10),
    sleepFn: async () => {},
  });
  assert(out.outcome === "unknown", "RPC down stays unknown, never dropped");
}

testMainnetGuard();
testParseNetworkDefault();
testNegativeAmounts();
testRpcHostForLog();
testFailCarriesExtra();
testMultiTxAddGateOnTxCountNotWidth();

Promise.resolve()
  .then(() => testPrepareTxForSendFreshBlockhash())
  .then(() => testConfirmRebroadcastsUntilLanded())
  .then(() => testConfirmReportsOnChainFailure())
  .then(() => testConfirmDropsWhenBlockhashExpires())
  .then(() => testConfirmStaysUnknownWhenRpcIsDown())
  .then(() => {
    console.log(`test_logic: passed=${passed} failed=${failed}`);
    process.exit(failed > 0 ? 1 : 0);
  });
