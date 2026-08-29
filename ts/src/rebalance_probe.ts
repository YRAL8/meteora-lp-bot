/**
 * Devnet-only probe for DLMM ``rebalancePosition``.
 * Signs/sends like exec.ts — never usable on a live mainnet wallet.
 *
 * Human messages → stderr. Exactly one JSON object → stdout.
 * Wallet secret is never logged. RPC host is logged without query/key.
 */
import {
  ComputeBudgetProgram,
  Connection,
  Keypair,
  PublicKey,
  Transaction,
  TransactionInstruction,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
import BN from "bn.js";
import DLMM, {
  MAX_ACTIVE_BIN_SLIPPAGE,
  MAX_RESIZE_LENGTH,
  ResizeSide,
  StrategyType,
  type LbPosition,
} from "@meteora-ag/dlmm";
import {
  eprint,
  fail,
  flagNum,
  flagStr,
  parseArgs,
  prepareTxForSend,
  rpcHostForLog,
} from "./build_lib";
import { explorerTxUrl } from "./network";
import { loadWalletKeypair } from "./wallet";

type Json = Record<string, unknown>;

const DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
const DEVNET_POOL_DEFAULT = "FRTZiQqJig2ib5Urico3yKsQikGG8EERM8mjK94D4o8q";
const WSOL_MINT = "So11111111111111111111111111111111111111112";
const COMMANDS = [
  "rebalance-quote",
  "rebalance-exec",
  "snapshot",
  "resize-exec",
] as const;
type ProbeCommand = (typeof COMMANDS)[number];

const COMPUTE_BUDGET_PROGRAM_ID = ComputeBudgetProgram.programId;

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function isProbeCommand(cmd: string): cmd is ProbeCommand {
  return (COMMANDS as readonly string[]).includes(cmd);
}

function bnish(v: unknown): string {
  if (v == null) return "0";
  if (typeof v === "bigint") return v.toString();
  if (typeof v === "number") return String(v);
  if (typeof v === "string") return v;
  if (typeof v === "object" && v !== null && "toString" in v) {
    return (v as { toString: () => string }).toString();
  }
  return String(v);
}

function amountToUi(raw: unknown, decimals: number): number {
  const s = bnish(raw);
  if (!s || s === "0") return 0;
  const neg = s.startsWith("-");
  const digits = neg ? s.slice(1) : s;
  const pad = digits.padStart(decimals + 1, "0");
  const whole = pad.slice(0, pad.length - decimals);
  const frac = pad.slice(pad.length - decimals);
  return Number(`${neg ? "-" : ""}${whole}.${frac}`);
}

function uiToAmount(ui: number, decimals: number): BN {
  if (!(ui >= 0) || !Number.isFinite(ui)) {
    throw new Error(`uiToAmount: amount must be finite and >= 0, got ${ui}`);
  }
  const s = ui.toFixed(decimals);
  const [whole, frac = ""] = s.split(".");
  const fracPadded = (frac + "0".repeat(decimals)).slice(0, decimals);
  const raw = whole + fracPadded;
  return new BN(raw.replace(/^0+(?=\d)/, "") || "0");
}

function strategyFromName(name: string | undefined): StrategyType {
  const n = (name || "Spot").toLowerCase();
  if (n === "spot") return StrategyType.Spot;
  if (n === "curve") return StrategyType.Curve;
  if (n === "bidask" || n === "bid-ask") return StrategyType.BidAsk;
  fail("?", `unknown --strategy ${name}`, "parseArgs");
}

function stripComputeBudget(tx: Transaction): Transaction {
  const out = new Transaction();
  for (const ix of tx.instructions) {
    if (!ix.programId.equals(COMPUTE_BUDGET_PROGRAM_ID)) out.add(ix);
  }
  return out;
}

function injectComputeBudget(
  tx: Transaction,
  units: number,
  priorityFeeMicrolamports: number
): Transaction {
  const stripped = stripComputeBudget(tx);
  const out = new Transaction();
  out.add(
    ComputeBudgetProgram.setComputeUnitLimit({ units }),
    ComputeBudgetProgram.setComputeUnitPrice({
      microLamports: priorityFeeMicrolamports,
    })
  );
  for (const ix of stripped.instructions) out.add(ix);
  return out;
}

function ixsToTx(ixs: TransactionInstruction[]): Transaction {
  const tx = new Transaction();
  for (const ix of ixs) tx.add(ix);
  return tx;
}

function toV0(tx: Transaction): VersionedTransaction {
  if (!tx.feePayer || !tx.recentBlockhash) {
    throw new Error("toV0: tx missing feePayer/blockhash");
  }
  const msg = new TransactionMessage({
    payerKey: tx.feePayer,
    recentBlockhash: tx.recentBlockhash,
    instructions: tx.instructions,
  }).compileToV0Message();
  return new VersionedTransaction(msg);
}

async function simulateV0(
  connection: Connection,
  vtx: VersionedTransaction,
  index: number
): Promise<{
  index: number;
  err: unknown;
  unitsConsumed: number | null;
  logsTail: string[];
}> {
  const result = await connection.simulateTransaction(vtx, {
    sigVerify: false,
    replaceRecentBlockhash: true,
    commitment: "confirmed",
  });
  const logs = result.value.logs || [];
  return {
    index,
    err: result.value.err,
    unitsConsumed: result.value.unitsConsumed ?? null,
    logsTail: logs.slice(-12),
  };
}

type ConfirmOutcome = "confirmed" | "failed" | "unknown";

async function pollSignatureStatus(
  connection: Connection,
  signature: string,
  timeoutMs = 90_000,
  pollMs = 2_000
): Promise<{
  outcome: ConfirmOutcome;
  slot: number | null;
  error: string | null;
}> {
  const start = Date.now();
  let lastError: string | null = null;
  while (Date.now() - start < timeoutMs) {
    try {
      const resp = await connection.getSignatureStatuses([signature], {
        searchTransactionHistory: true,
      });
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
    } catch (err) {
      lastError = err instanceof Error ? err.message : String(err);
    }
    await sleep(pollMs);
  }
  return { outcome: "unknown", slot: null, error: lastError };
}

function solUsdcOrder(dlmm: DLMM): {
  solIsX: boolean;
  solDecimals: number;
  usdcDecimals: number;
} {
  const x = dlmm.tokenX.publicKey.toBase58();
  const y = dlmm.tokenY.publicKey.toBase58();
  const solIsX = x === WSOL_MINT;
  if (!solIsX && y !== WSOL_MINT) {
    fail("rebalance-probe", `pool has no WSOL mint (X=${x} Y=${y})`, "pool");
  }
  return {
    solIsX,
    solDecimals: solIsX ? dlmm.tokenX.mint.decimals : dlmm.tokenY.mint.decimals,
    usdcDecimals: solIsX ? dlmm.tokenY.mint.decimals : dlmm.tokenX.mint.decimals,
  };
}

async function snapshotPosition(
  connection: Connection,
  dlmm: DLMM,
  owner: PublicKey,
  positionStr: string
): Promise<Json> {
  const order = solUsdcOrder(dlmm);
  const { userPositions } = await dlmm.getPositionsByUserAndLbPair(owner);
  const hit = userPositions.find(
    (p: LbPosition) => p.publicKey.toBase58() === positionStr
  );
  const solLamports = await connection.getBalance(owner, "confirmed");
  let usdcUi = 0;
  const usdcMint = new PublicKey(
    order.solIsX
      ? dlmm.tokenY.publicKey.toBase58()
      : dlmm.tokenX.publicKey.toBase58()
  );
  try {
    const { getAssociatedTokenAddressSync, getAccount } = await import(
      "@solana/spl-token"
    );
    const ata = getAssociatedTokenAddressSync(usdcMint, owner);
    const acc = await getAccount(connection, ata, "confirmed");
    usdcUi = amountToUi(acc.amount.toString(), order.usdcDecimals);
  } catch {
    usdcUi = 0;
  }
  const active = await dlmm.getActiveBin();
  const wallet = {
    sol: solLamports / 1e9,
    solLamports,
    usdc: usdcUi,
  };
  if (!hit) {
    return {
      found: false,
      position: positionStr,
      activeId: dlmm.lbPair.activeId,
      wallet,
    };
  }
  const d = hit.positionData;
  const xUi = amountToUi(d.totalXAmount, dlmm.tokenX.mint.decimals);
  const yUi = amountToUi(d.totalYAmount, dlmm.tokenY.mint.decimals);
  const feeXUi = amountToUi(d.feeX, dlmm.tokenX.mint.decimals);
  const feeYUi = amountToUi(d.feeY, dlmm.tokenY.mint.decimals);
  const sol = order.solIsX ? xUi : yUi;
  const usdc = order.solIsX ? yUi : xUi;
  const priceRaw = Number(active.price.toString());
  const exp = order.solDecimals - order.usdcDecimals;
  const usdcPerSol = order.solIsX
    ? priceRaw * 10 ** exp
    : 10 ** -exp / (priceRaw || 1);
  const activeId = dlmm.lbPair.activeId;
  const bins = binMap(
    d.positionBinData || [],
    order,
    dlmm,
    activeId,
    usdcPerSol
  );
  return {
    found: true,
    position: positionStr,
    lowerBinId: d.lowerBinId,
    upperBinId: d.upperBinId,
    width: d.upperBinId - d.lowerBinId + 1,
    activeId,
    activeBinPrice: active.price.toString(),
    usdcPerSol: Number.isFinite(usdcPerSol) ? usdcPerSol : null,
    sol,
    usdc,
    valueUsdc: sol * (Number.isFinite(usdcPerSol) ? usdcPerSol : 0) + usdc,
    fees: {
      sol: order.solIsX ? feeXUi : feeYUi,
      usdc: order.solIsX ? feeYUi : feeXUi,
    },
    raw: {
      totalXAmount: bnish(d.totalXAmount),
      totalYAmount: bnish(d.totalYAmount),
      feeX: bnish(d.feeX),
      feeY: bnish(d.feeY),
    },
    bins,
    wallet,
  };
}

function binMap(
  rows: Array<{
    binId: number;
    positionXAmount: string;
    positionYAmount: string;
  }>,
  order: { solIsX: boolean },
  dlmm: DLMM,
  activeId: number,
  usdcPerSol: number
): Json {
  const xDec = dlmm.tokenX.mint.decimals;
  const yDec = dlmm.tokenY.mint.decimals;
  const bins: Json[] = [];
  let below = 0;
  let at = 0;
  let above = 0;
  let total = 0;
  let nonempty = 0;
  for (const row of rows) {
    const xUi = amountToUi(row.positionXAmount, xDec);
    const yUi = amountToUi(row.positionYAmount, yDec);
    const sol = order.solIsX ? xUi : yUi;
    const usdc = order.solIsX ? yUi : xUi;
    const valueUsdc =
      sol * (Number.isFinite(usdcPerSol) ? usdcPerSol : 0) + usdc;
    const side =
      row.binId < activeId ? "below" : row.binId > activeId ? "above" : "active";
    if (sol > 0 || usdc > 0) nonempty += 1;
    total += valueUsdc;
    if (side === "below") below += valueUsdc;
    else if (side === "above") above += valueUsdc;
    else at += valueUsdc;
    bins.push({
      binId: row.binId,
      sol,
      usdc,
      valueUsdc,
      side,
    });
  }
  const safe = total > 0 ? total : 1;
  return {
    count: bins.length,
    nonempty,
    activeId,
    valueUsdc: { below, active: at, above, total },
    share: {
      below: below / safe,
      active: at / safe,
      above: above / safe,
    },
    bins,
  };
}

function serializeSimulation(sim: {
  actualAmountXWithdrawn: BN;
  actualAmountYWithdrawn: BN;
  actualAmountXDeposited: BN;
  actualAmountYDeposited: BN;
  amountXDeposited: BN;
  amountYDeposited: BN;
  rewardAmountsClaimed: BN[];
  rentalCostLamports: BN;
  depositParams: unknown[];
  withdrawParams: unknown[];
}): Json {
  return {
    actualAmountXWithdrawn: bnish(sim.actualAmountXWithdrawn),
    actualAmountYWithdrawn: bnish(sim.actualAmountYWithdrawn),
    actualAmountXDeposited: bnish(sim.actualAmountXDeposited),
    actualAmountYDeposited: bnish(sim.actualAmountYDeposited),
    amountXDeposited: bnish(sim.amountXDeposited),
    amountYDeposited: bnish(sim.amountYDeposited),
    rewardAmountsClaimed: (sim.rewardAmountsClaimed || []).map(bnish),
    rentalCostLamports: bnish(sim.rentalCostLamports),
    depositCount: (sim.depositParams || []).length,
    withdrawCount: (sim.withdrawParams || []).length,
    depositParams: (sim.depositParams || []).map((p) => {
      const row = p as Record<string, unknown>;
      return {
        minDeltaId: bnish(row.minDeltaId ?? row.minBinId),
        maxDeltaId: bnish(row.maxDeltaId ?? row.maxBinId),
      };
    }),
    withdrawParams: (sim.withdrawParams || []).map((p) => {
      const row = p as Record<string, unknown>;
      return {
        minBinId: bnish(row.minBinId),
        maxBinId: bnish(row.maxBinId),
        bps: bnish(row.bps),
      };
    }),
  };
}

async function assertDevnetOnly(
  flags: Record<string, string | boolean>,
  connection: Connection,
  action: string
): Promise<void> {
  const raw = flags["network"];
  const network = raw === undefined || raw === true ? "" : String(raw).toLowerCase();
  if (network !== "devnet") {
    fail(
      action,
      "this probe is hard-wired to --network devnet (got " +
        JSON.stringify(raw ?? "(missing)") +
        "); it refuses any other network, including via METEORA_ALLOW_MAINNET",
      "network"
    );
  }
  const genesis = await connection.getGenesisHash();
  if (genesis !== DEVNET_GENESIS) {
    fail(
      action,
      `RPC genesis ${genesis} is not Solana devnet (${DEVNET_GENESIS}) — refuse`,
      "network"
    );
  }
}

async function compileGroup(
  connection: Connection,
  owner: PublicKey,
  ixs: TransactionInstruction[],
  index: number,
  priorityFee: number
): Promise<{ vtx: VersionedTransaction; sim: Json; ixCount: number }> {
  if (ixs.length === 0) {
    throw new Error(`compileGroup: no instructions for group ${index}`);
  }
  let tx = ixsToTx(ixs);
  const { blockhash } = await connection.getLatestBlockhash("confirmed");
  tx.feePayer = owner;
  tx.recentBlockhash = blockhash;
  const probeVtx = toV0(tx);
  const probe = await simulateV0(connection, probeVtx, index);
  const probeCu = probe.unitsConsumed ?? 200_000;
  const cuLimit = Math.ceil(probeCu * 1.2);
  tx = injectComputeBudget(tx, cuLimit, priorityFee);
  const { blockhash: bh2 } = await connection.getLatestBlockhash("confirmed");
  tx.feePayer = owner;
  tx.recentBlockhash = bh2;
  const vtx = toV0(tx);
  const sim = await simulateV0(connection, vtx, index);
  return {
    vtx,
    ixCount: ixs.length,
    sim: {
      ...sim,
      cuLimit,
      probeCu,
    },
  };
}

async function sendVtx(
  connection: Connection,
  wallet: Keypair,
  vtxIn: VersionedTransaction,
  index: number
): Promise<Json> {
  const prepared = await prepareTxForSend(
    vtxIn,
    index,
    [wallet],
    async () => connection.getLatestBlockhash("confirmed")
  );
  const vtx = prepared.vtx;
  const signature = prepared.signature;
  eprint(`sending tx[${index}] signature=${signature}`);
  await connection.sendRawTransaction(vtx.serialize(), {
    skipPreflight: true,
    maxRetries: 3,
  });
  const { outcome, slot, error } = await pollSignatureStatus(
    connection,
    signature
  );
  return {
    index,
    signature,
    status: outcome,
    slot,
    error,
    explorer: explorerTxUrl(signature, "devnet"),
  };
}

async function runResize(args: {
  cmd: string;
  flags: Record<string, string | boolean>;
  connection: Connection;
  dlmm: DLMM;
  wallet: Keypair;
  owner: PublicKey;
  pubkey: string;
  poolStr: string;
  positionStr: string;
  before: Json;
  send: boolean;
  priorityFee: number;
}): Promise<void> {
  const {
    cmd,
    flags,
    connection,
    dlmm,
    wallet,
    owner,
    pubkey,
    poolStr,
    positionStr,
    before,
    send,
    priorityFee,
  } = args;
  const op = (flagStr(flags, "resize-op") || "").toLowerCase();
  if (op !== "increase" && op !== "decrease") {
    fail(cmd, "--resize-op must be increase|decrease", "parseArgs");
  }
  const sideName = (flagStr(flags, "resize-side") || "").toLowerCase();
  if (sideName !== "lower" && sideName !== "upper") {
    fail(cmd, "--resize-side must be lower|upper", "parseArgs");
  }
  const length = flagNum(flags, "resize-length");
  if (length == null || length < 1 || !Number.isInteger(length)) {
    fail(cmd, "--resize-length must be a positive integer", "parseArgs");
  }
  const maxOne = Number(bnish(MAX_RESIZE_LENGTH));
  if (length > maxOne) {
    fail(
      cmd,
      `--resize-length ${length} exceeds MAX_RESIZE_LENGTH=${maxOne}`,
      "parseArgs"
    );
  }
  const side = sideName === "lower" ? ResizeSide.Lower : ResizeSide.Upper;
  eprint(
    `cmd=${cmd} op=${op} side=${sideName} length=${length} ` +
      `mode=${send ? "send" : "simulate"} position=${positionStr}`
  );

  let txs: Transaction[] | undefined;
  try {
    if (op === "increase") {
      txs = await dlmm.increasePositionLength(
        new PublicKey(positionStr),
        side,
        new BN(length),
        owner,
        true
      );
    } else {
      txs = await dlmm.decreasePositionLength(
        new PublicKey(positionStr),
        side,
        new BN(length),
        true
      );
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    fail(cmd, msg, "build", { before, op, side: sideName, length });
  }
  if (!txs || !txs.length) {
    fail(cmd, `${op} returned no transactions`, "build", { before });
  }

  const compiled: Json[] = [];
  const vtxs: VersionedTransaction[] = [];
  for (let i = 0; i < txs.length; i++) {
    try {
      const c = await compileGroup(
        connection,
        owner,
        txs[i].instructions,
        i,
        priorityFee
      );
      vtxs.push(c.vtx);
      compiled.push({ index: i, ixCount: c.ixCount, simulation: c.sim });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      compiled.push({ index: i, error: msg });
    }
  }

  const base: Json = {
    ok: true,
    action: cmd,
    network: "devnet",
    owner: pubkey,
    pool: poolStr,
    position: positionStr,
    params: { op, side: sideName, length, maxResizeLength: maxOne },
    before,
    txCount: txs.length,
    compiled,
    dryRun: !send,
  };

  if (!send) {
    process.stdout.write(JSON.stringify(base) + "\n");
    return;
  }
  if (vtxs.length !== txs.length) {
    fail(cmd, "some resize txs failed to compile — not sending", "build", base);
  }

  const sends: Json[] = [];
  const signatures: string[] = [];
  for (let i = 0; i < vtxs.length; i++) {
    try {
      const row = await sendVtx(connection, wallet, vtxs[i], i);
      sends.push(row);
      if (typeof row.signature === "string") signatures.push(row.signature);
      if (row.status !== "confirmed") {
        fail(cmd, `tx[${i}] status=${row.status}`, "confirm", {
          ...base,
          signatures,
          sends,
        });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      fail(cmd, msg, "send", { ...base, signatures, sends });
    }
    await sleep(800);
  }

  await sleep(1500);
  await dlmm.refetchStates();
  const after = await snapshotPosition(connection, dlmm, owner, positionStr);
  process.stdout.write(
    JSON.stringify({
      ...base,
      dryRun: false,
      sent: true,
      signatures,
      sends,
      after,
    }) + "\n"
  );
}

async function main(): Promise<void> {
  const { cmd, flags } = parseArgs(process.argv.slice(2));
  if (!isProbeCommand(cmd)) {
    fail(
      cmd,
      "unknown command (want rebalance-quote | rebalance-exec | snapshot | resize-exec)",
      "parseArgs"
    );
  }

  const send = flags["send"] === true;
  if (send && cmd !== "rebalance-exec" && cmd !== "resize-exec") {
    fail(cmd, "--send is only valid on rebalance-exec | resize-exec", "parseArgs");
  }

  const rpc =
    flagStr(flags, "rpc") ||
    process.env.SOLANA_DEVNET_RPC_URL ||
    "https://api.devnet.solana.com";
  const poolStr = flagStr(flags, "pool", DEVNET_POOL_DEFAULT)!;
  const connection = new Connection(rpc, "confirmed");

  await assertDevnetOnly(flags, connection, cmd);

  const { keypair: wallet, pubkey } = loadWalletKeypair(
    flagStr(flags, "wallet")
  );
  const owner = new PublicKey(pubkey);
  const pool = new PublicKey(poolStr);
  const positionStr = flagStr(flags, "position");
  if (!positionStr) fail(cmd, "missing --position", "parseArgs");

  const strategyName = flagStr(flags, "strategy", "Spot")!;
  const strategy = strategyFromName(strategyName);
  const xWithdrawBps = flagNum(flags, "x-withdraw-bps") ?? 10_000;
  const yWithdrawBps = flagNum(flags, "y-withdraw-bps") ?? 10_000;
  const topUpSol = flagNum(flags, "top-up-sol") ?? 0;
  const topUpUsdc = flagNum(flags, "top-up-usdc") ?? 0;
  const maxActiveBinSlippage =
    flagNum(flags, "max-active-bin-slippage") ?? MAX_ACTIVE_BIN_SLIPPAGE;
  const slippageBps = flagNum(flags, "slippage-bps") ?? 100;
  const slippagePct = slippageBps / 100;
  const priorityFee = flagNum(flags, "priority-fee") ?? 50_000;
  const sendGroup = (flagStr(flags, "send-group", "all") || "all").toLowerCase();
  if (!["all", "init", "rebalance"].includes(sendGroup)) {
    fail(cmd, "--send-group must be all|init|rebalance", "parseArgs");
  }

  eprint(
    `cmd=${cmd} network=devnet rpc=${rpcHostForLog(rpc)} pool=${poolStr} ` +
      `wallet=${pubkey} position=${positionStr} strategy=${strategyName} ` +
      `xWithdrawBps=${xWithdrawBps} yWithdrawBps=${yWithdrawBps} ` +
      `topUpSol=${topUpSol} topUpUsdc=${topUpUsdc} ` +
      `mode=${send ? "send" : "simulate"} sendGroup=${sendGroup}`
  );

  let dlmm: DLMM;
  try {
    dlmm = await DLMM.create(connection, pool);
    await dlmm.refetchStates();
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    fail(cmd, msg, "DLMM.create");
  }

  const before = await snapshotPosition(connection, dlmm, owner, positionStr);
  if (!before.found) {
    fail(cmd, `position ${positionStr} not found for wallet ${pubkey}`, "position");
  }

  const { userPositions } = await dlmm.getPositionsByUserAndLbPair(owner);
  const pos = userPositions.find(
    (p: LbPosition) => p.publicKey.toBase58() === positionStr
  );
  if (!pos) fail(cmd, `position ${positionStr} disappeared`, "position");

  if (cmd === "snapshot") {
    process.stdout.write(
      JSON.stringify({
        ok: true,
        action: cmd,
        network: "devnet",
        owner: pubkey,
        pool: poolStr,
        position: positionStr,
        before,
      }) + "\n"
    );
    return;
  }

  if (cmd === "resize-exec") {
    await runResize({
      cmd,
      flags,
      connection,
      dlmm,
      wallet,
      owner,
      pubkey,
      poolStr,
      positionStr,
      before,
      send,
      priorityFee,
    });
    return;
  }

  const order = solUsdcOrder(dlmm);
  const topUpX = order.solIsX
    ? uiToAmount(topUpSol, order.solDecimals)
    : uiToAmount(topUpUsdc, order.usdcDecimals);
  const topUpY = order.solIsX
    ? uiToAmount(topUpUsdc, order.usdcDecimals)
    : uiToAmount(topUpSol, order.solDecimals);

  let quote: Awaited<
    ReturnType<DLMM["simulateRebalancePositionWithBalancedStrategy"]>
  >;
  try {
    quote = await dlmm.simulateRebalancePositionWithBalancedStrategy(
      pos.publicKey,
      pos.positionData,
      strategy,
      topUpX,
      topUpY,
      new BN(xWithdrawBps),
      new BN(yWithdrawBps)
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    fail(cmd, msg, "simulate", { before });
  }

  const sim = quote.simulationResult;
  const simulatedWindow = {
    lowerBinId: Number(bnish(quote.rebalancePosition.lowerBinId)),
    upperBinId: Number(bnish(quote.rebalancePosition.upperBinId)),
    width:
      Number(bnish(quote.rebalancePosition.upperBinId)) -
      Number(bnish(quote.rebalancePosition.lowerBinId)) +
      1,
  };

  let built: {
    initBinArrayInstructions: TransactionInstruction[];
    rebalancePositionInstruction: TransactionInstruction[];
  };
  try {
    built = await dlmm.rebalancePosition(
      quote,
      new BN(maxActiveBinSlippage),
      owner,
      slippagePct
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    fail(cmd, msg, "build", {
      before,
      simulatedWindow,
      simulation: serializeSimulation(sim),
    });
  }

  const initIxs = built.initBinArrayInstructions || [];
  const rebalanceIxs = built.rebalancePositionInstruction || [];
  const groups: { name: string; ixs: TransactionInstruction[] }[] = [];
  if (initIxs.length) groups.push({ name: "initBinArray", ixs: initIxs });
  if (rebalanceIxs.length) {
    groups.push({ name: "rebalancePosition", ixs: rebalanceIxs });
  }

  const compiled: Json[] = [];
  const vtxByName = new Map<string, VersionedTransaction>();
  for (let i = 0; i < groups.length; i++) {
    const g = groups[i];
    try {
      const c = await compileGroup(connection, owner, g.ixs, i, priorityFee);
      vtxByName.set(g.name, c.vtx);
      compiled.push({
        name: g.name,
        index: i,
        ixCount: c.ixCount,
        simulation: c.sim,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      compiled.push({ name: g.name, index: i, error: msg });
    }
  }

  // Try packing both groups into one tx when both exist.
  let combined: Json | null = null;
  if (initIxs.length && rebalanceIxs.length) {
    try {
      const c = await compileGroup(
        connection,
        owner,
        [...initIxs, ...rebalanceIxs],
        99,
        priorityFee
      );
      combined = {
        possible: true,
        ixCount: c.ixCount,
        simulation: c.sim,
      };
    } catch (err) {
      combined = {
        possible: false,
        error: err instanceof Error ? err.message : String(err),
      };
    }
  } else {
    combined = {
      possible: true,
      reason: initIxs.length === 0 ? "initBinArrayInstructions empty" : "single group",
    };
  }

  const rental = {
    binArrayCount: quote.binArrayCount,
    binArrayCost: quote.binArrayCost,
    bitmapExtensionCost: quote.bitmapExtensionCost,
    rentalCostLamports: bnish(sim.rentalCostLamports),
  };

  const base: Json = {
    ok: true,
    action: cmd,
    network: "devnet",
    owner: pubkey,
    pool: poolStr,
    position: positionStr,
    params: {
      strategy: strategyName,
      xWithdrawBps,
      yWithdrawBps,
      topUpSol,
      topUpUsdc,
      maxActiveBinSlippage,
      slippageBps,
      slippagePct,
      shouldClaimFee:
        "balanced strategy hard-codes shouldClaimFee=true / shouldClaimReward=true",
    },
    before,
    simulatedWindow,
    simulation: serializeSimulation(sim),
    rental,
    instructionGroups: {
      initBinArrayCount: initIxs.length,
      rebalanceIxCount: rebalanceIxs.length,
      twoOnChainGroups: initIxs.length > 0 && rebalanceIxs.length > 0,
      combined,
      compiled,
    },
    dryRun: !send,
  };

  if (!send) {
    process.stdout.write(JSON.stringify(base) + "\n");
    return;
  }

  const toSend: { name: string; vtx: VersionedTransaction; index: number }[] = [];
  for (let i = 0; i < groups.length; i++) {
    const g = groups[i];
    if (sendGroup === "init" && g.name !== "initBinArray") continue;
    if (sendGroup === "rebalance" && g.name !== "rebalancePosition") continue;
    const vtx = vtxByName.get(g.name);
    if (!vtx) {
      fail(cmd, `group ${g.name} failed to compile — not sending`, "build", base);
    }
    toSend.push({ name: g.name, vtx, index: i });
  }
  if (!toSend.length) {
    fail(
      cmd,
      `nothing to send (sendGroup=${sendGroup}, init=${initIxs.length}, rebalance=${rebalanceIxs.length})`,
      "send",
      base
    );
  }

  const sends: Json[] = [];
  const signatures: string[] = [];
  for (const item of toSend) {
    try {
      const row = await sendVtx(connection, wallet, item.vtx, item.index);
      sends.push({ ...row, name: item.name });
      if (typeof row.signature === "string") signatures.push(row.signature);
      if (row.status !== "confirmed") {
        fail(cmd, `tx[${item.index}] ${item.name} status=${row.status}`, "confirm", {
          ...base,
          signatures,
          sends,
        });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      fail(cmd, msg, "send", { ...base, signatures, sends });
    }
    await sleep(800);
  }

  await sleep(1500);
  await dlmm.refetchStates();
  const after = await snapshotPosition(connection, dlmm, owner, positionStr);

  process.stdout.write(
    JSON.stringify({
      ...base,
      dryRun: false,
      sent: true,
      sendGroup,
      signatures,
      sends,
      after,
    }) + "\n"
  );
}

main().catch((err) => {
  const msg = err instanceof Error ? err.message : String(err);
  fail("rebalance-probe", msg, "runtime");
});
