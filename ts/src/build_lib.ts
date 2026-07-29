/**
 * Shared Meteora DLMM build + simulate logic (no sign/send).
 * Used by cli.ts (dry-run) and exec.ts (sign/send).
 */
import {
  Connection,
  ComputeBudgetProgram,
  Keypair,
  PublicKey,
  Transaction,
  TransactionMessage,
  VersionedTransaction,
} from "@solana/web3.js";
import {
  ASSOCIATED_TOKEN_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  getAccount,
  getAssociatedTokenAddressSync,
} from "@solana/spl-token";
import BN from "bn.js";
import DLMM, {
  StrategyType,
  POSITION_MIN_SIZE,
  MAX_BIN_LENGTH_ALLOWED_IN_ONE_TX,
  DEFAULT_BIN_PER_POSITION,
  autoFillXByStrategy,
  autoFillYByStrategy,
  calculatePositionSize,
  chunkBinRange,
  getPriceOfBinByBinId,
  type LbPosition,
} from "@meteora-ag/dlmm";

export const DEFAULT_POOL = "5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6";
export const DEFAULT_RPC =
  process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
export const DEFAULT_RANGE_HALF = 34; // activeId ± 34 → 69 bins
/**
 * Unified slippage default in basis points for open/add/swap.
 * SDK liquidity methods take percent → convert with bps/100 at the call site.
 */
const DEFAULT_SLIPPAGE_BPS = 100; // 1%
/** Default priority fee in microlamports per CU. */
const DEFAULT_PRIORITY_FEE_MICROLAMPORTS = 0;
/** SOL left untouched for future tx fees when reporting balances. */
const DEFAULT_FEE_RESERVE_SOL = 0.02;
/** Anchor account discriminator length missing from calculatePositionSize. */
const ACCOUNT_DISCRIMINATOR_BYTES = 8;

const WSOL_MINT = new PublicKey(
  "So11111111111111111111111111111111111111112"
);
const COMPUTE_BUDGET_PROGRAM_ID = ComputeBudgetProgram.programId;

type Json = Record<string, unknown>;

export function eprint(...args: unknown[]): void {
  console.error(...args);
}

export function fail(action: string, error: string, stage: string): never {
  process.stdout.write(
    JSON.stringify({ ok: false, action, error, stage }) + "\n"
  );
  process.exit(1);
}

export function parseArgs(argv: string[]): {
  cmd: string;
  flags: Record<string, string | boolean>;
} {
  const [cmd, ...rest] = argv;
  if (!cmd || cmd.startsWith("-")) {
    fail("?", "missing command", "parseArgs");
  }
  const flags: Record<string, string | boolean> = {};
  for (let i = 0; i < rest.length; i++) {
    const a = rest[i];
    if (!a.startsWith("--")) continue;
    const key = a.slice(2);
    const next = rest[i + 1];
    if (next === undefined || next.startsWith("--")) {
      flags[key] = true;
    } else {
      flags[key] = next;
      i++;
    }
  }
  return { cmd, flags };
}

export function flagStr(
  flags: Record<string, string | boolean>,
  name: string,
  fallback?: string
): string | undefined {
  const v = flags[name];
  if (v === undefined || v === true) return fallback;
  return String(v);
}

export function flagNum(
  flags: Record<string, string | boolean>,
  name: string
): number | undefined {
  const s = flagStr(flags, name);
  if (s === undefined) return undefined;
  const n = Number(s);
  if (!Number.isFinite(n)) fail("?", `--${name} not a number: ${s}`, "parseArgs");
  return n;
}

export function requireFlag(
  flags: Record<string, string | boolean>,
  name: string,
  action: string
): string {
  const v = flagStr(flags, name);
  if (!v) fail(action, `missing --${name}`, "parseArgs");
  return v;
}

/** Reject removed `--slippage` (percent) — only `--slippage-bps` is allowed. */
function rejectLegacySlippageFlag(
  flags: Record<string, string | boolean>,
  action: string
): void {
  if (Object.prototype.hasOwnProperty.call(flags, "slippage")) {
    fail(
      action,
      "--slippage removed; use --slippage-bps (basis points, 100 = 1%)",
      "parseArgs"
    );
  }
}

export function requireSlippageBps(
  flags: Record<string, string | boolean>,
  action: string
): number {
  rejectLegacySlippageFlag(flags, action);
  const v = flagNum(flags, "slippage-bps");
  const bps = v === undefined ? DEFAULT_SLIPPAGE_BPS : v;
  if (!Number.isInteger(bps) || bps < 1 || bps > 10000) {
    fail(action, "--slippage-bps must be an integer 1..10000", "parseArgs");
  }
  return bps;
}

/** SDK liquidity `slippage` is percent; CLI speaks bps only. */
function slippageBpsToSdkPercent(bps: number): number {
  return bps / 100;
}

export function requirePriorityFee(
  flags: Record<string, string | boolean>,
  action: string
): number {
  const v = flagNum(flags, "priority-fee");
  const fee = v === undefined ? DEFAULT_PRIORITY_FEE_MICROLAMPORTS : v;
  if (fee < 0) fail(action, "--priority-fee must be >= 0", "parseArgs");
  return fee;
}

export function validateDepositAmounts(
  solUi: number | undefined,
  usdcUi: number | undefined
): string | null {
  if (solUi === undefined || usdcUi === undefined) {
    return "need --sol and --usdc";
  }
  if (solUi < 0) return "--sol must be >= 0";
  if (usdcUi < 0) return "--usdc must be >= 0";
  if (solUi === 0 && usdcUi === 0) {
    return "--sol and --usdc cannot both be 0";
  }
  return null;
}

export function requireDepositAmounts(
  flags: Record<string, string | boolean>,
  action: string
): { solUi: number; usdcUi: number } {
  const solUi = flagNum(flags, "sol");
  const usdcUi = flagNum(flags, "usdc");
  const err = validateDepositAmounts(solUi, usdcUi);
  if (err) fail(action, err, "parseArgs");
  return { solUi: solUi!, usdcUi: usdcUi! };
}

function positionAccountSizeBytes(binCount: number): {
  sdkSize: number;
  onChainSize: number;
} {
  const sdkSize = calculatePositionSize(new BN(binCount)).toNumber();
  return {
    sdkSize,
    onChainSize: sdkSize + ACCOUNT_DISCRIMINATOR_BYTES,
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function withRpcRetry<T>(
  label: string,
  fn: () => Promise<T>,
  retries = 6
): Promise<T> {
  let last: unknown;
  for (let i = 0; i < retries; i++) {
    try {
      return await fn();
    } catch (err) {
      last = err;
      const msg = err instanceof Error ? err.message : String(err);
      const low = msg.toLowerCase();
      const is429 =
        msg.includes("429") ||
        low.includes("too many requests") ||
        msg.includes("rate limit");
      // Обрыв связи с узлом столь же временен, как и 429, но раньше улетал
      // наружу сразу: пользователь видел голое "fetch failed" и жал кнопку
      // заново. Повторяем те же ошибки, что и лимит частоты.
      const isTransientNetwork =
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
        low.includes("504");
      if ((!is429 && !isTransientNetwork) || i === retries - 1) throw err;
      const pause = 400 * Math.pow(2, i) + Math.floor(Math.random() * 200);
      eprint(
        `RPC retry (${is429 ? "429" : "network"}) on ${label}, sleep ${pause}ms`
      );
      await sleep(pause);
    }
  }
  throw last;
}

function strategyFromName(name: string | undefined): StrategyType {
  const n = (name || "Spot").toLowerCase();
  if (n === "spot") return StrategyType.Spot;
  if (n === "curve") return StrategyType.Curve;
  if (n === "bidask" || n === "bid-ask") return StrategyType.BidAsk;
  fail("?", `unknown strategy ${name}`, "parseArgs");
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

function amountToUi(
  raw: BN | string | number | bigint,
  decimals: number
): number {
  const s = typeof raw === "bigint" ? raw.toString() : raw.toString();
  const neg = s.startsWith("-");
  const digits = neg ? s.slice(1) : s;
  const padded = digits.padStart(decimals + 1, "0");
  const whole = padded.slice(0, padded.length - decimals) || "0";
  const frac = padded.slice(padded.length - decimals);
  return Number(`${neg ? "-" : ""}${whole}.${frac}`);
}

function toTxArray(tx: Transaction | Transaction[]): Transaction[] {
  return Array.isArray(tx) ? tx : [tx];
}

function programIdsOfIxs(
  ixs: { programId: PublicKey }[]
): string[] {
  const ids = new Set<string>();
  for (const ix of ixs) ids.add(ix.programId.toBase58());
  return [...ids];
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

async function prepareLegacyTx(
  connection: Connection,
  tx: Transaction,
  feePayer: PublicKey
): Promise<Transaction> {
  const { blockhash, lastValidBlockHeight } = await withRpcRetry(
    "getLatestBlockhash",
    () => connection.getLatestBlockhash("confirmed")
  );
  tx.feePayer = feePayer;
  tx.recentBlockhash = blockhash;
  tx.lastValidBlockHeight = lastValidBlockHeight;
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

function v0Signers(vtx: VersionedTransaction): string[] {
  const keys = vtx.message.staticAccountKeys;
  const n = vtx.message.header.numRequiredSignatures;
  const out: string[] = [];
  for (let i = 0; i < n; i++) out.push(keys[i].toBase58());
  return out;
}

function v0AccountsCount(vtx: VersionedTransaction): number {
  return vtx.message.staticAccountKeys.length;
}

type SimResult = {
  index: number;
  err: unknown;
  unitsConsumed: number | null;
  logsTail: string[];
};

async function simulateV0(
  connection: Connection,
  vtx: VersionedTransaction,
  index: number
): Promise<SimResult> {
  const result = await withRpcRetry(`simulate#${index}`, () =>
    connection.simulateTransaction(vtx, {
      sigVerify: false,
      replaceRecentBlockhash: true,
      commitment: "confirmed",
    })
  );
  const value = result.value;
  const logs = value.logs || [];
  return {
    index,
    err: value.err,
    unitsConsumed: value.unitsConsumed ?? null,
    logsTail: logs.slice(-8),
  };
}

function describeV0(vtx: VersionedTransaction, index: number): Json {
  return {
    index,
    kind: "v0",
    numInstructions: vtx.message.compiledInstructions.length,
    programIds: programIdsOfIxs(
      // Resolve program ids from static keys via compiled ix programIdIndex.
      vtx.message.compiledInstructions.map((ci) => ({
        programId: vtx.message.staticAccountKeys[ci.programIdIndex],
      }))
    ),
    signers: v0Signers(vtx),
    accountsCount: v0AccountsCount(vtx),
    base64: Buffer.from(vtx.serialize()).toString("base64"),
  };
}

async function loadDlmm(
  connection: Connection,
  pool: PublicKey
): Promise<DLMM> {
  const dlmm = await withRpcRetry("DLMM.create", () =>
    DLMM.create(connection, pool)
  );
  await withRpcRetry("refetchStates", () => dlmm.refetchStates());
  return dlmm;
}

function solUsdcOrder(dlmm: DLMM): {
  solIsX: boolean;
  solDecimals: number;
  usdcDecimals: number;
  solMint: string;
  usdcMint: string;
} {
  const x = dlmm.tokenX.publicKey.toBase58();
  const y = dlmm.tokenY.publicKey.toBase58();
  const solIsX = x === WSOL_MINT.toBase58();
  if (!solIsX && y !== WSOL_MINT.toBase58()) {
    fail("pool-info", `pool has no WSOL mint (X=${x} Y=${y})`, "pool");
  }
  return {
    solIsX,
    solDecimals: solIsX ? dlmm.tokenX.mint.decimals : dlmm.tokenY.mint.decimals,
    usdcDecimals: solIsX ? dlmm.tokenY.mint.decimals : dlmm.tokenX.mint.decimals,
    solMint: solIsX ? x : y,
    usdcMint: solIsX ? y : x,
  };
}

function amountsFromUi(
  dlmm: DLMM,
  solUi: number,
  usdcUi: number
): { totalXAmount: BN; totalYAmount: BN; order: ReturnType<typeof solUsdcOrder> } {
  const order = solUsdcOrder(dlmm);
  const solRaw = uiToAmount(solUi, order.solDecimals);
  const usdcRaw = uiToAmount(usdcUi, order.usdcDecimals);
  return {
    totalXAmount: order.solIsX ? solRaw : usdcRaw,
    totalYAmount: order.solIsX ? usdcRaw : solRaw,
    order,
  };
}

function assertPositionOwner(
  position: LbPosition,
  owner: PublicKey,
  action: string
): void {
  const real = position.positionData.owner.toBase58();
  const want = owner.toBase58();
  if (real !== want) {
    fail(
      action,
      `position ${position.publicKey.toBase58()} owned by ${real}, not ${want}`,
      "check"
    );
  }
}

/**
 * Probe-simulate → set CU limit = ceil(probe*1.2) + priority fee → convert to v0
 * → final simulate. Serialized base64 is the v0 bytes B3 should send.
 */
async function finalizeBuild(
  connection: Connection,
  action: string,
  owner: PublicKey,
  pool: PublicKey,
  params: Json,
  txsIn: Transaction[],
  notes: string[],
  priorityFeeMicrolamports: number
): Promise<Json> {
  const txsMeta: Json[] = [];
  const simulation: SimResult[] = [];
  const cuNotes: string[] = [];

  for (let i = 0; i < txsIn.length; i++) {
    const preparedProbe = await prepareLegacyTx(connection, txsIn[i], owner);
    const vtxProbe = toV0(preparedProbe);
    const probe = await simulateV0(connection, vtxProbe, i);
    const probeCu = probe.unitsConsumed ?? 200_000;
    const cuLimit = Math.ceil(probeCu * 1.2);

    const withBudget = injectComputeBudget(
      preparedProbe,
      cuLimit,
      priorityFeeMicrolamports
    );
    const prepared = await prepareLegacyTx(connection, withBudget, owner);
    const vtx = toV0(prepared);
    txsMeta.push(describeV0(vtx, i));
    const sim = await simulateV0(connection, vtx, i);
    simulation.push(sim);

    cuNotes.push(
      `tx[${i}] compute budget: setComputeUnitLimit=${cuLimit} ` +
        `(probe unitsConsumed=${probeCu} + 20% headroom); ` +
        `setComputeUnitPrice=${priorityFeeMicrolamports} microlamports/CU. ` +
        `Existing SDK ComputeBudget ixs stripped and replaced so B3 sends what we simulated.`
    );
  }

  notes.push(
    "TX kind=v0: legacy SDK Transaction is compiled to VersionedTransaction (v0) " +
      "before simulate+serialize. web3.js config form of simulateTransaction " +
      "(sigVerify:false) only types against VersionedTransaction; the deprecated " +
      "legacy overload does not accept SimulateTransactionConfig. B3 must sign/send " +
      "this same v0 object (txs[].base64 / kind=v0), not the raw SDK legacy Transaction."
  );
  notes.push(...cuNotes);

  const simFailed = simulation.some((s) => s.err != null);
  const body: Json = {
    ok: !simFailed,
    action,
    owner: owner.toBase58(),
    pool: pool.toBase58(),
    params,
    txs: txsMeta,
    simulation,
    notes,
  };
  if (simFailed) {
    body.stage = "simulate";
    body.error = `simulation failed on tx index(es): ${simulation
      .filter((s) => s.err != null)
      .map((s) => s.index)
      .join(",")}`;
  }
  return body;
}

export async function cmdPoolInfo(
  connection: Connection,
  pool: PublicKey
): Promise<Json> {
  const dlmm = await loadDlmm(connection, pool);
  const active = await withRpcRetry("getActiveBin", () => dlmm.getActiveBin());
  const order = solUsdcOrder(dlmm);
  const pricePerLamport = Number(active.price);
  const price = Number(dlmm.fromPricePerLamport(pricePerLamport));
  // fromPricePerLamport: price of tokenX in tokenY per 1 whole tokenX.
  // For SOL-USDC with X=WSOL → USDC per 1 SOL.
  const usdcPerSol = order.solIsX
    ? price
    : price === 0
      ? null
      : 1 / price;

  const reserveX = amountToUi(dlmm.tokenX.amount, dlmm.tokenX.mint.decimals);
  const reserveY = amountToUi(dlmm.tokenY.amount, dlmm.tokenY.mint.decimals);

  return {
    ok: true,
    action: "pool-info",
    pool: pool.toBase58(),
    activeId: dlmm.lbPair.activeId,
    binStep: dlmm.lbPair.binStep,
    tokenX: {
      mint: dlmm.tokenX.publicKey.toBase58(),
      decimals: dlmm.tokenX.mint.decimals,
      reserve: reserveX,
    },
    tokenY: {
      mint: dlmm.tokenY.publicKey.toBase58(),
      decimals: dlmm.tokenY.mint.decimals,
      reserve: reserveY,
    },
    usdcPerSol,
    priceFromActiveBin: usdcPerSol,
    activeBinPriceLamport: active.price.toString(),
    // Protocol max bins for one ordinary position (SDK DEFAULT_BIN_PER_POSITION).
    maxBinsPerPosition: DEFAULT_BIN_PER_POSITION.toNumber(),
    reserves: {
      sol: order.solIsX ? reserveX : reserveY,
      usdc: order.solIsX ? reserveY : reserveX,
    },
  };
}

export async function cmdListPositions(
  connection: Connection,
  pool: PublicKey,
  ownerStr: string
): Promise<Json> {
  const owner = new PublicKey(ownerStr);
  const dlmm = await loadDlmm(connection, pool);
  const order = solUsdcOrder(dlmm);
  const { userPositions } = await withRpcRetry("getPositionsByUserAndLbPair", () =>
    dlmm.getPositionsByUserAndLbPair(owner)
  );

  const positions = userPositions.map((p: LbPosition) => {
    const d = p.positionData;
    const xUi = amountToUi(d.totalXAmount, dlmm.tokenX.mint.decimals);
    const yUi = amountToUi(d.totalYAmount, dlmm.tokenY.mint.decimals);
    const feeXUi = amountToUi(d.feeX, dlmm.tokenX.mint.decimals);
    const feeYUi = amountToUi(d.feeY, dlmm.tokenY.mint.decimals);
    return {
      pubkey: p.publicKey.toBase58(),
      lowerBinId: d.lowerBinId,
      upperBinId: d.upperBinId,
      width: d.upperBinId - d.lowerBinId + 1,
      sol: order.solIsX ? xUi : yUi,
      usdc: order.solIsX ? yUi : xUi,
      fees: {
        sol: order.solIsX ? feeXUi : feeYUi,
        usdc: order.solIsX ? feeYUi : feeXUi,
      },
      raw: {
        totalXAmount: d.totalXAmount.toString(),
        totalYAmount: d.totalYAmount.toString(),
        feeX: d.feeX.toString(),
        feeY: d.feeY.toString(),
      },
    };
  });

  return {
    ok: true,
    action: "list-positions",
    owner: owner.toBase58(),
    pool: pool.toBase58(),
    count: positions.length,
    positions,
  };
}

export async function cmdBalances(
  connection: Connection,
  pool: PublicKey,
  ownerStr: string
): Promise<Json> {
  const owner = new PublicKey(ownerStr);
  const dlmm = await loadDlmm(connection, pool);
  const order = solUsdcOrder(dlmm);
  const usdcMint = new PublicKey(order.usdcMint);

  const solLamports = await withRpcRetry("getBalance", () =>
    connection.getBalance(owner, "confirmed")
  );
  const ownerInfo = await withRpcRetry("getAccountInfo(owner)", () =>
    connection.getAccountInfo(owner, "confirmed")
  );
  const ownerDataLen = ownerInfo?.data.length ?? 0;
  const rentExemptOwner = await withRpcRetry(
    "getMinimumBalanceForRentExemption(owner)",
    () => connection.getMinimumBalanceForRentExemption(ownerDataLen)
  );

  const usdcAta = getAssociatedTokenAddressSync(
    usdcMint,
    owner,
    false,
    TOKEN_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID
  );
  const wsolAta = getAssociatedTokenAddressSync(
    WSOL_MINT,
    owner,
    false,
    TOKEN_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID
  );

  async function readAta(ata: PublicKey): Promise<{
    exists: boolean;
    amountUi: number;
    amountRaw: string;
    rentLamports: number | null;
  }> {
    try {
      const acc = await withRpcRetry(`getAccount(${ata.toBase58()})`, () =>
        getAccount(connection, ata, "confirmed", TOKEN_PROGRAM_ID)
      );
      const info = await withRpcRetry(`getAccountInfo(${ata.toBase58()})`, () =>
        connection.getAccountInfo(ata, "confirmed")
      );
      const decimals =
        ata.equals(wsolAta) ? order.solDecimals : order.usdcDecimals;
      return {
        exists: true,
        amountUi: amountToUi(acc.amount.toString(), decimals),
        amountRaw: acc.amount.toString(),
        rentLamports: info?.lamports ?? null,
      };
    } catch {
      return {
        exists: false,
        amountUi: 0,
        amountRaw: "0",
        rentLamports: null,
      };
    }
  }

  const usdc = await readAta(usdcAta);
  const wsol = await readAta(wsolAta);

  const solUi = solLamports / 1e9;
  const feeReserveSol = DEFAULT_FEE_RESERVE_SOL;
  const solAfterFeeReserve = Math.max(0, solUi - feeReserveSol);
  // Native wallet rent-exempt minimum is locked while the account exists.
  const rentLockedLamports = rentExemptOwner;
  const ataRentLamports =
    (usdc.rentLamports ?? 0) + (wsol.rentLamports ?? 0);

  // Default open range width = 2*DEFAULT_RANGE_HALF+1 bins; rent uses on-chain size (+8 disc).
  const defaultBinCount = DEFAULT_RANGE_HALF * 2 + 1;
  const { sdkSize, onChainSize } = positionAccountSizeBytes(defaultBinCount);
  const positionRentLamports = await withRpcRetry(
    "getMinimumBalanceForRentExemption(position+disc)",
    () => connection.getMinimumBalanceForRentExemption(onChainSize)
  );
  const positionRentSol = positionRentLamports / 1e9;
  const solAvailableForOpen = Math.max(
    0,
    solUi - feeReserveSol - positionRentSol
  );

  return {
    ok: true,
    action: "balances",
    owner: owner.toBase58(),
    pool: pool.toBase58(),
    sol: {
      lamports: solLamports,
      ui: solUi,
      rentExemptMinimumLamports: rentExemptOwner,
      rentLockedLamports,
      note:
        "rentLockedLamports = rent-exempt minimum for the system account itself; " +
        "ATA rents are separate (see ata.*.rentLamports).",
    },
    usdc: {
      mint: order.usdcMint,
      ui: usdc.amountUi,
      raw: usdc.amountRaw,
    },
    ata: {
      usdc: {
        address: usdcAta.toBase58(),
        exists: usdc.exists,
        rentLamports: usdc.rentLamports,
      },
      wsol: {
        address: wsolAta.toBase58(),
        exists: wsol.exists,
        amountUi: wsol.amountUi,
        rentLamports: wsol.rentLamports,
      },
    },
    feeReserveSol,
    solAfterFeeReserve,
    positionRentForDefaultOpen: {
      binCount: defaultBinCount,
      sdkSizeBytes: sdkSize,
      onChainSizeBytes: onChainSize,
      lamports: positionRentLamports,
      sol: positionRentSol,
    },
    solAvailableForOpen,
    ataRentLamportsTotal: ataRentLamports,
    notes: [
      `feeReserveSol=${feeReserveSol} (DEFAULT_FEE_RESERVE_SOL) — tx fee headroom only`,
      `position rent for default ${defaultBinCount}-bin open: ${positionRentLamports} lamports (${positionRentSol} SOL) from on-chain size ${onChainSize} (= sdk ${sdkSize} + ${ACCOUNT_DISCRIMINATOR_BYTES} discriminator); rent returns to wallet when position is closed`,
      `solAvailableForOpen = sol - feeReserve - positionRent = ${solAvailableForOpen}`,
    ],
  };
}

async function loadPosition(
  dlmm: DLMM,
  positionStr: string
): Promise<LbPosition> {
  return withRpcRetry("getPosition", () =>
    dlmm.getPosition(new PublicKey(positionStr))
  );
}

export async function cmdBuildOpen(
  connection: Connection,
  pool: PublicKey,
  flags: Record<string, string | boolean>,
  execOpts?: { positionKeypair: Keypair }
): Promise<Json> {
  const action = "build-open";
  const owner = new PublicKey(requireFlag(flags, "owner", action));
  const { solUi, usdcUi } = requireDepositAmounts(flags, action);
  const strategyType = strategyFromName(flagStr(flags, "strategy", "Spot"));
  const slippageBps = requireSlippageBps(flags, action);
  const slippagePct = slippageBpsToSdkPercent(slippageBps);
  const priorityFee = requirePriorityFee(flags, action);

  const dlmm = await loadDlmm(connection, pool);
  const activeId = dlmm.lbPair.activeId;
  let minBinId = flagNum(flags, "min-bin-id");
  let maxBinId = flagNum(flags, "max-bin-id");
  if (minBinId === undefined || maxBinId === undefined) {
    minBinId = activeId - DEFAULT_RANGE_HALF;
    maxBinId = activeId + DEFAULT_RANGE_HALF;
  }
  if (minBinId > maxBinId) fail(action, "min-bin-id > max-bin-id", "parseArgs");

  const { totalXAmount, totalYAmount, order } = amountsFromUi(
    dlmm,
    solUi,
    usdcUi
  );

  const externalPk = flagStr(flags, "position-pubkey");
  let positionPubKey: PublicKey;
  let positionKeypairEphemeral: boolean;
  if (execOpts?.positionKeypair) {
    positionPubKey = execOpts.positionKeypair.publicKey;
    positionKeypairEphemeral = true;
  } else if (externalPk) {
    positionPubKey = new PublicKey(externalPk);
    positionKeypairEphemeral = false;
  } else {
    // Ephemeral keypair stays in this process only — secret never serialized.
    positionPubKey = Keypair.generate().publicKey;
    positionKeypairEphemeral = true;
  }
  const binCount = maxBinId - minBinId + 1;

  const tx = await withRpcRetry("initializePositionAndAddLiquidityByStrategy", () =>
    dlmm.initializePositionAndAddLiquidityByStrategy({
      positionPubKey,
      totalXAmount,
      totalYAmount,
      strategy: { minBinId, maxBinId, strategyType },
      user: owner,
      slippage: slippagePct,
    })
  );
  const txs = toTxArray(tx);

  const { sdkSize, onChainSize } = positionAccountSizeBytes(binCount);
  const rentSdkSize = await withRpcRetry("rent(sdkSize)", () =>
    connection.getMinimumBalanceForRentExemption(sdkSize)
  );
  const rentOnChain = await withRpcRetry("rent(onChainSize)", () =>
    connection.getMinimumBalanceForRentExemption(onChainSize)
  );

  const notes = [
    positionKeypairEphemeral
      ? `positionPubkey=${positionPubKey.toBase58()} positionKeypairEphemeral=true — эту сборку невозможно подписать позже, она годится только как проверка (секрет эфемерного ключа не сохраняется и никуда не выводится)`
      : `positionPubkey=${positionPubKey.toBase58()} from --position-pubkey (caller holds the secret; not present in this process)`,
    `binRange=[${minBinId}, ${maxBinId}] width=${binCount} (default half-width ${DEFAULT_RANGE_HALF} around activeId=${activeId})`,
    `Required signers for open: (1) owner/feePayer ${owner.toBase58()}, (2) position keypair ${positionPubKey.toBase58()}`,
    `SDK method: initializePositionAndAddLiquidityByStrategy; slippageBps=${slippageBps} (DEFAULT_SLIPPAGE_BPS=${DEFAULT_SLIPPAGE_BPS}) → SDK percent=${slippagePct}`,
    `txs returned: ${txs.length}`,
    `position size: calculatePositionSize=${sdkSize} bytes (POSITION_MIN_SIZE=${POSITION_MIN_SIZE}); on-chain account size=${onChainSize} (= sdk + ${ACCOUNT_DISCRIMINATOR_BYTES} discriminator)`,
    `position rent: at sdkSize ${rentSdkSize} lamports; at onChainSize ${rentOnChain} lamports (Δ=${rentOnChain - rentSdkSize}); use on-chain figure ≈ ${rentOnChain / 1e9} SOL`,
    `amounts: ${solUi} SOL + ${usdcUi} USDC → X=${totalXAmount.toString()} Y=${totalYAmount.toString()} (solIsX=${order.solIsX})`,
  ];

  return finalizeBuild(
    connection,
    action,
    owner,
    pool,
    {
      sol: solUi,
      usdc: usdcUi,
      minBinId,
      maxBinId,
      strategy: StrategyType[strategyType],
      positionPubkey: positionPubKey.toBase58(),
      positionKeypairEphemeral,
      activeId,
      slippageBps,
      priorityFeeMicrolamports: priorityFee,
      positionSizeSdkBytes: sdkSize,
      positionSizeOnChainBytes: onChainSize,
      positionRentLamports: rentOnChain,
    },
    txs,
    notes,
    priorityFee
  );
}

export async function cmdBuildAdd(
  connection: Connection,
  pool: PublicKey,
  flags: Record<string, string | boolean>
): Promise<Json> {
  const action = "build-add";
  const owner = new PublicKey(requireFlag(flags, "owner", action));
  const positionStr = requireFlag(flags, "position", action);
  const { solUi, usdcUi } = requireDepositAmounts(flags, action);
  const strategyType = strategyFromName(flagStr(flags, "strategy", "Spot"));
  const slippageBps = requireSlippageBps(flags, action);
  const slippagePct = slippageBpsToSdkPercent(slippageBps);
  const priorityFee = requirePriorityFee(flags, action);
  const allowMultiTx = flags["allow-multi-tx"] === true;

  const dlmm = await loadDlmm(connection, pool);
  const position = await loadPosition(dlmm, positionStr);
  assertPositionOwner(position, owner, action);
  const { lowerBinId, upperBinId } = position.positionData;
  const { totalXAmount, totalYAmount, order } = amountsFromUi(
    dlmm,
    solUi,
    usdcUi
  );

  // SDK exports MAX_BIN_LENGTH_ALLOWED_IN_ONE_TX (=26): max bins in a single
  // add-liquidity TX before the SDK requires the chunkable path (account/size limits).
  const width = upperBinId - lowerBinId + 1;
  const maxBinsOneTx = MAX_BIN_LENGTH_ALLOWED_IN_ONE_TX;
  let txs: Transaction[];
  let method: string;
  if (width > maxBinsOneTx) {
    method = "addLiquidityByStrategyChunkable";
    txs = await withRpcRetry(method, () =>
      dlmm.addLiquidityByStrategyChunkable({
        positionPubKey: position.publicKey,
        totalXAmount,
        totalYAmount,
        strategy: {
          minBinId: lowerBinId,
          maxBinId: upperBinId,
          strategyType,
        },
        user: owner,
        slippage: slippagePct,
      })
    );
  } else {
    method = "addLiquidityByStrategy";
    const tx = await withRpcRetry(method, () =>
      dlmm.addLiquidityByStrategy({
        positionPubKey: position.publicKey,
        totalXAmount,
        totalYAmount,
        strategy: {
          minBinId: lowerBinId,
          maxBinId: upperBinId,
          strategyType,
        },
        user: owner,
        slippage: slippagePct,
      })
    );
    txs = toTxArray(tx);
  }

  const chunks = chunkBinRange(lowerBinId, upperBinId, maxBinsOneTx) as Array<{
    lowerBinId: number;
    upperBinId: number;
  }>;
  const chunkNotes = chunks.map(
    (c, i) =>
      `chunk[${i}] bins [${c.lowerBinId}, ${c.upperBinId}] width=${c.upperBinId - c.lowerBinId + 1}`
  );

  // Wide ranges use the chunkable path (even when SDK currently returns 1 tx).
  // Require an explicit opt-in: partial landing of a multi-tx series is dangerous.
  if ((txs.length > 1 || width > maxBinsOneTx) && !allowMultiTx) {
    return {
      ok: false,
      action,
      stage: "multi-tx",
      error:
        `refusing add for width=${width} (txs=${txs.length}) without --allow-multi-tx; ` +
        `width > MAX_BIN_LENGTH_ALLOWED_IN_ONE_TX (${maxBinsOneTx}) uses chunkable path ` +
        `(partial landing leaves an intermediate position)`,
      owner: owner.toBase58(),
      pool: pool.toBase58(),
      params: {
        position: positionStr,
        sol: solUi,
        usdc: usdcUi,
        strategy: StrategyType[strategyType],
        lowerBinId,
        upperBinId,
        width,
        slippageBps,
        priorityFeeMicrolamports: priorityFee,
        maxBinsOneTx,
        txCount: txs.length,
      },
      txs: [],
      simulation: [],
      notes: [
        `SDK method: ${method}`,
        `position range [${lowerBinId}, ${upperBinId}] width=${width}`,
        ...chunkNotes,
        "Pass --allow-multi-tx only if you accept that landing a prefix of chunks leaves partial liquidity.",
      ],
    };
  }

  const notes = [
    `SDK method: ${method}`,
    `position range [${lowerBinId}, ${upperBinId}] width=${width}`,
    `chunkable threshold: width > MAX_BIN_LENGTH_ALLOWED_IN_ONE_TX (${maxBinsOneTx})`,
    `amounts SOL=${solUi} USDC=${usdcUi} (solIsX=${order.solIsX}); slippageBps=${slippageBps} (SDK percent=${slippagePct}, DEFAULT_SLIPPAGE_BPS=${DEFAULT_SLIPPAGE_BPS})`,
    "Required signer: owner only (existing position account).",
    allowMultiTx && width > maxBinsOneTx
      ? `allow-multi-tx enabled: txs=${txs.length}. If only a prefix of a multi-tx series lands, later chunks never run — position stays partially filled. Expected chunk coverage (chunkBinRange): ${chunkNotes.join("; ")}`
      : txs.length > 1
        ? `multiple txs=${txs.length}; simulated independently. Chunk coverage: ${chunkNotes.join("; ")}`
        : "single transaction",
  ];

  return finalizeBuild(
    connection,
    action,
    owner,
    pool,
    {
      position: positionStr,
      sol: solUi,
      usdc: usdcUi,
      strategy: StrategyType[strategyType],
      lowerBinId,
      upperBinId,
      slippageBps,
      priorityFeeMicrolamports: priorityFee,
      maxBinsOneTx,
      allowMultiTx,
    },
    txs,
    notes,
    priorityFee
  );
}

export async function cmdBuildClaimFees(
  connection: Connection,
  pool: PublicKey,
  flags: Record<string, string | boolean>
): Promise<Json> {
  const action = "build-claim-fees";
  const owner = new PublicKey(requireFlag(flags, "owner", action));
  const positionStr = requireFlag(flags, "position", action);
  const priorityFee = requirePriorityFee(flags, action);
  const dlmm = await loadDlmm(connection, pool);
  const position = await loadPosition(dlmm, positionStr);
  assertPositionOwner(position, owner, action);

  const tx = await withRpcRetry("claimSwapFee", () =>
    dlmm.claimSwapFee({ owner, position })
  );
  const txs = toTxArray(tx);

  return finalizeBuild(
    connection,
    action,
    owner,
    pool,
    { position: positionStr, priorityFeeMicrolamports: priorityFee },
    txs,
    [
      "SDK method: claimSwapFee",
      `feeX=${position.positionData.feeX.toString()} feeY=${position.positionData.feeY.toString()}`,
      "Required signer: owner",
    ],
    priorityFee
  );
}

export async function cmdBuildWithdraw(
  connection: Connection,
  pool: PublicKey,
  flags: Record<string, string | boolean>
): Promise<Json> {
  const action = "build-withdraw";
  const owner = new PublicKey(requireFlag(flags, "owner", action));
  const positionStr = requireFlag(flags, "position", action);
  const bps = flagNum(flags, "bps");
  if (bps === undefined) fail(action, "missing --bps", "parseArgs");
  if (bps < 1 || bps > 10000) {
    fail(action, "--bps must be 1..10000", "parseArgs");
  }
  const priorityFee = requirePriorityFee(flags, action);

  const dlmm = await loadDlmm(connection, pool);
  const position = await loadPosition(dlmm, positionStr);
  assertPositionOwner(position, owner, action);
  const { lowerBinId, upperBinId } = position.positionData;

  const txs = await withRpcRetry("removeLiquidity", () =>
    dlmm.removeLiquidity({
      user: owner,
      position: position.publicKey,
      fromBinId: lowerBinId,
      toBinId: upperBinId,
      bps: new BN(bps),
      shouldClaimAndClose: false,
    })
  );

  return finalizeBuild(
    connection,
    action,
    owner,
    pool,
    { position: positionStr, bps, priorityFeeMicrolamports: priorityFee },
    toTxArray(txs),
    [
      "SDK method: removeLiquidity (shouldClaimAndClose=false)",
      `withdraw ${bps} bps of liquidity across bins [${lowerBinId}, ${upperBinId}]`,
      "Required signer: owner",
    ],
    priorityFee
  );
}

export async function cmdBuildClose(
  connection: Connection,
  pool: PublicKey,
  flags: Record<string, string | boolean>
): Promise<Json> {
  const action = "build-close";
  const owner = new PublicKey(requireFlag(flags, "owner", action));
  const positionStr = requireFlag(flags, "position", action);
  const priorityFee = requirePriorityFee(flags, action);

  const dlmm = await loadDlmm(connection, pool);
  const position = await loadPosition(dlmm, positionStr);
  assertPositionOwner(position, owner, action);
  const { lowerBinId, upperBinId } = position.positionData;

  const txs = await withRpcRetry("removeLiquidity+close", () =>
    dlmm.removeLiquidity({
      user: owner,
      position: position.publicKey,
      fromBinId: lowerBinId,
      toBinId: upperBinId,
      bps: new BN(10_000),
      shouldClaimAndClose: true,
    })
  );

  return finalizeBuild(
    connection,
    action,
    owner,
    pool,
    { position: positionStr, priorityFeeMicrolamports: priorityFee },
    toTxArray(txs),
    [
      "SDK method: removeLiquidity with bps=10000 and shouldClaimAndClose=true (withdraw 100% + claim fees + close)",
      `bins [${lowerBinId}, ${upperBinId}]`,
      "Required signer: owner",
    ],
    priorityFee
  );
}

/** Close a zero-liquidity position (rent reclaim). Do not use removeLiquidity. */
export async function cmdBuildCloseEmpty(
  connection: Connection,
  pool: PublicKey,
  flags: Record<string, string | boolean>
): Promise<Json> {
  const action = "build-close-empty";
  const owner = new PublicKey(requireFlag(flags, "owner", action));
  const positionStr = requireFlag(flags, "position", action);
  const priorityFee = requirePriorityFee(flags, action);

  const dlmm = await loadDlmm(connection, pool);
  const position = await loadPosition(dlmm, positionStr);
  assertPositionOwner(position, owner, action);

  const totalX = position.positionData.totalXAmount.toString();
  const totalY = position.positionData.totalYAmount.toString();
  if (totalX !== "0" || totalY !== "0") {
    fail(
      action,
      `position still has liquidity (totalX=${totalX}, totalY=${totalY}); use build-close / removeLiquidity`,
      "precheck"
    );
  }

  const tx = await withRpcRetry("closePositionIfEmpty", () =>
    dlmm.closePositionIfEmpty({ owner, position })
  );

  return finalizeBuild(
    connection,
    action,
    owner,
    pool,
    { position: positionStr, priorityFeeMicrolamports: priorityFee },
    toTxArray(tx),
    [
      "SDK method: closePositionIfEmpty (zero-liquidity rent reclaim)",
      "Required signer: owner",
    ],
    priorityFee
  );
}

export async function cmdBuildSwap(
  connection: Connection,
  pool: PublicKey,
  flags: Record<string, string | boolean>
): Promise<Json> {
  const action = "build-swap";
  const owner = new PublicKey(requireFlag(flags, "owner", action));
  const side = requireFlag(flags, "side", action);
  if (side !== "sol-to-usdc" && side !== "usdc-to-sol") {
    fail(action, "--side must be sol-to-usdc|usdc-to-sol", "parseArgs");
  }
  const amountUi = flagNum(flags, "amount");
  if (amountUi === undefined) fail(action, "missing --amount", "parseArgs");
  if (!(amountUi > 0)) {
    fail(action, "--amount must be > 0", "parseArgs");
  }
  const slippageBps = requireSlippageBps(flags, action);
  const priorityFee = requirePriorityFee(flags, action);

  const dlmm = await loadDlmm(connection, pool);
  const order = solUsdcOrder(dlmm);
  const startActiveId = dlmm.lbPair.activeId;

  // swapForY=true means X→Y. For this pool X=WSOL, Y=USDC.
  const solToUsdc = side === "sol-to-usdc";
  const swapForY = order.solIsX ? solToUsdc : !solToUsdc;
  const inDecimals = solToUsdc ? order.solDecimals : order.usdcDecimals;
  const outDecimals = solToUsdc ? order.usdcDecimals : order.solDecimals;
  const inAmount = uiToAmount(amountUi, inDecimals);
  const inToken = solToUsdc
    ? new PublicKey(order.solMint)
    : new PublicKey(order.usdcMint);
  const outToken = solToUsdc
    ? new PublicKey(order.usdcMint)
    : new PublicKey(order.solMint);

  const binArrays = await withRpcRetry("getBinArrayForSwap", () =>
    dlmm.getBinArrayForSwap(swapForY)
  );
  const quote = dlmm.swapQuote(
    inAmount,
    swapForY,
    new BN(slippageBps),
    binArrays
  );

  const startPriceQ = getPriceOfBinByBinId(startActiveId, dlmm.lbPair.binStep);
  const endPriceQ = quote.endPrice;
  const startP = Number(startPriceQ.toString());
  const endP = Number(endPriceQ.toString());
  const step = 1 + dlmm.lbPair.binStep / 10_000;
  const binsMovedApprox =
    startP > 0 && endP > 0
      ? Math.round(Math.log(endP / startP) / Math.log(step))
      : null;

  const tx = await withRpcRetry("swap", () =>
    dlmm.swap({
      inToken,
      outToken,
      inAmount,
      minOutAmount: quote.minOutAmount,
      lbPair: pool,
      user: owner,
      binArraysPubkey: quote.binArraysPubkey,
    })
  );

  const inUi = amountToUi(quote.consumedInAmount, inDecimals);
  const outUi = amountToUi(quote.outAmount, outDecimals);
  const minOutUi = amountToUi(quote.minOutAmount, outDecimals);
  const feeUi = amountToUi(
    quote.fee,
    quote.feeOnInput ? inDecimals : outDecimals
  );

  const notes = [
    "SDK method: getBinArrayForSwap → swapQuote → swap",
    `side=${side} swapForY=${swapForY} (X→Y when true); solIsX=${order.solIsX}`,
    `in: ${inUi} (${side === "sol-to-usdc" ? "SOL" : "USDC"}) raw=${quote.consumedInAmount.toString()}`,
    `out quoted: ${outUi} (${side === "sol-to-usdc" ? "USDC" : "SOL"}) raw=${quote.outAmount.toString()}`,
    `minOut (slippageBps=${slippageBps}, DEFAULT_SLIPPAGE_BPS=${DEFAULT_SLIPPAGE_BPS}): ${minOutUi} raw=${quote.minOutAmount.toString()}`,
    `pool fee raw=${quote.fee.toString()} (feeOnInput=${quote.feeOnInput}) ≈ ${feeUi}; protocolFee=${quote.protocolFee.toString()}`,
    `activeId start=${startActiveId}; endPrice(Q)=${endPriceQ.toString()}; binsMovedApprox=${binsMovedApprox}; priceImpact%=${quote.priceImpact.toString()}`,
    "Required signer: owner",
  ];

  return finalizeBuild(
    connection,
    action,
    owner,
    pool,
    {
      side,
      amount: amountUi,
      slippageBps,
      priorityFeeMicrolamports: priorityFee,
      swapForY,
      inAmountRaw: inAmount.toString(),
      consumedInAmount: quote.consumedInAmount.toString(),
      outAmount: quote.outAmount.toString(),
      minOutAmount: quote.minOutAmount.toString(),
      fee: quote.fee.toString(),
      protocolFee: quote.protocolFee.toString(),
      feeOnInput: quote.feeOnInput,
      priceImpactPct: quote.priceImpact.toString(),
      endPrice: quote.endPrice.toString(),
      startActiveId,
      binsMovedApprox,
      inUi,
      outUi,
      minOutUi,
      feeUi,
    },
    toTxArray(tx),
    notes,
    priorityFee
  );
}

/**
 * Suggest SOL/USDC deposit amounts for a Spot range via SDK autoFill*,
 * plus a swap hint to move the wallet toward that mix.
 */
export async function cmdSuggestAmounts(
  connection: Connection,
  pool: PublicKey,
  flags: Record<string, string | boolean>
): Promise<Json> {
  const action = "suggest-amounts";
  const owner = new PublicKey(requireFlag(flags, "owner", action));
  const strategyType = strategyFromName(flagStr(flags, "strategy", "Spot"));
  const budgetSol = flagNum(flags, "budget-sol");
  const budgetUsdc = flagNum(flags, "budget-usdc");
  if (budgetSol === undefined && budgetUsdc === undefined) {
    fail(action, "need --budget-sol and/or --budget-usdc", "parseArgs");
  }
  if (budgetSol !== undefined && budgetSol < 0) {
    fail(action, "--budget-sol must be >= 0", "parseArgs");
  }
  if (budgetUsdc !== undefined && budgetUsdc < 0) {
    fail(action, "--budget-usdc must be >= 0", "parseArgs");
  }
  if ((budgetSol ?? 0) === 0 && (budgetUsdc ?? 0) === 0) {
    fail(action, "budget cannot be all zeros", "parseArgs");
  }

  const dlmm = await loadDlmm(connection, pool);
  const order = solUsdcOrder(dlmm);
  const activeId = dlmm.lbPair.activeId;
  const binStep = dlmm.lbPair.binStep;
  const half = flagNum(flags, "half-width") ?? DEFAULT_RANGE_HALF;
  let minBinId = flagNum(flags, "min-bin-id");
  let maxBinId = flagNum(flags, "max-bin-id");
  if (minBinId === undefined || maxBinId === undefined) {
    minBinId = activeId - half;
    maxBinId = activeId + half;
  }
  if (minBinId > maxBinId) fail(action, "min-bin-id > max-bin-id", "parseArgs");

  const active = await withRpcRetry("getActiveBin", () => dlmm.getActiveBin());
  const price = Number(dlmm.fromPricePerLamport(Number(active.price)));
  const usdcPerSol = order.solIsX ? price : price === 0 ? 0 : 1 / price;
  if (!(usdcPerSol > 0)) fail(action, "cannot price pool", "runtime");

  const zero = new BN(0);
  const amountXInActiveBin = zero;
  const amountYInActiveBin = zero;

  const rangeKind =
    maxBinId < activeId
      ? "entirely-below-active"
      : minBinId > activeId
        ? "entirely-above-active"
        : "straddles-active";

  const budgetSolUi = budgetSol ?? 0;
  const budgetUsdcUi = budgetUsdc ?? 0;
  const totalBudgetUsd = budgetSolUi * usdcPerSol + budgetUsdcUi;

  let needSol = 0;
  let needUsdc = 0;
  let targetSolFractionFinal = 0.5;

  if (rangeKind === "entirely-above-active") {
    // Only SOL for a range above the active bin.
    // totalBudgetUsd is the full position size (not a single-leg hint).
    if (!order.solIsX) {
      fail(action, "pool with SOL as tokenY not implemented for suggest-amounts", "runtime");
    }
    needSol = totalBudgetUsd / usdcPerSol;
    needUsdc = 0;
    targetSolFractionFinal = 1;
  } else if (rangeKind === "entirely-below-active") {
    if (!order.solIsX) {
      fail(action, "pool with SOL as tokenY not implemented for suggest-amounts", "runtime");
    }
    needSol = 0;
    needUsdc = totalBudgetUsd;
    targetSolFractionFinal = 0;
  } else if (order.solIsX) {
    // Straddling Spot: always treat budget as TOTAL position USD
    // (budgetSol*price + budgetUsdc), split by autoFill ratio.
    // Sole --budget-usdc N ⇒ position ≈ $N (not a $N USDC leg + matching SOL).
    const probeX = uiToAmount(1, order.solDecimals);
    const probeY = autoFillYByStrategy(
      activeId,
      binStep,
      probeX,
      amountXInActiveBin,
      amountYInActiveBin,
      minBinId,
      maxBinId,
      strategyType
    );
    const pSol = 1;
    const pUsdc = amountToUi(probeY, order.usdcDecimals);
    const pTotal = pSol * usdcPerSol + pUsdc;
    targetSolFractionFinal = pTotal > 0 ? (pSol * usdcPerSol) / pTotal : 0.5;
    needSol = (totalBudgetUsd * targetSolFractionFinal) / usdcPerSol;
    needUsdc = totalBudgetUsd * (1 - targetSolFractionFinal);
    const needSolUsd = needSol * usdcPerSol;
    const needTotalUsd = needSolUsd + needUsdc;
    if (needTotalUsd > 0) targetSolFractionFinal = needSolUsd / needTotalUsd;
  } else {
    fail(action, "pool with SOL as tokenY not implemented for suggest-amounts", "runtime");
  }

  const solLamports = await withRpcRetry("getBalance", () =>
    connection.getBalance(owner, "confirmed")
  );
  const haveSol = solLamports / 1e9;
  const usdcMint = new PublicKey(order.usdcMint);
  const usdcAta = getAssociatedTokenAddressSync(
    usdcMint,
    owner,
    false,
    TOKEN_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID
  );
  let haveUsdc = 0;
  try {
    const acc = await withRpcRetry("getAccount(usdc)", () =>
      getAccount(connection, usdcAta, "confirmed", TOKEN_PROGRAM_ID)
    );
    haveUsdc = amountToUi(acc.amount.toString(), order.usdcDecimals);
  } catch {
    haveUsdc = 0;
  }

  const feeReserve = DEFAULT_FEE_RESERVE_SOL;
  const usableSol = Math.max(0, haveSol - feeReserve);
  const deficitSol = Math.max(0, needSol - usableSol);
  const deficitUsdc = Math.max(0, needUsdc - haveUsdc);
  const surplusSol = Math.max(0, usableSol - needSol);
  const surplusUsdc = Math.max(0, haveUsdc - needUsdc);

  let swapSide: string | null = null;
  let swapAmount = 0;
  if (deficitSol > 1e-9 && surplusUsdc > 0) {
    swapSide = "usdc-to-sol";
    swapAmount = Math.min(surplusUsdc, deficitSol * usdcPerSol);
  } else if (deficitUsdc > 1e-6 && surplusSol > 0) {
    swapSide = "sol-to-usdc";
    swapAmount = Math.min(surplusSol, deficitUsdc / usdcPerSol);
  }

  return {
    ok: true,
    action,
    owner: owner.toBase58(),
    pool: pool.toBase58(),
    params: {
      minBinId,
      maxBinId,
      halfWidth: half,
      strategy: StrategyType[strategyType],
      budgetSol: budgetSol ?? null,
      budgetUsdc: budgetUsdc ?? null,
      activeId,
      binStep,
      usdcPerSol,
      rangeKind,
    },
    targetSolFraction: targetSolFractionFinal,
    needSol,
    needUsdc,
    balances: {
      sol: haveSol,
      usdc: haveUsdc,
      usableSolAfterFeeReserve: usableSol,
      feeReserveSol: feeReserve,
    },
    swapSuggestion:
      swapSide && swapAmount > 0
        ? {
            side: swapSide,
            amount: swapAmount,
            buildSwapArgs: [
              "build-swap",
              "--owner",
              owner.toBase58(),
              "--side",
              swapSide,
              "--amount",
              String(swapAmount),
            ],
          }
        : null,
    notes: [
      `SDK: autoFillYByStrategy / autoFillXByStrategy (strategy=${StrategyType[strategyType]})`,
      `range [${minBinId},${maxBinId}] vs activeId=${activeId} → ${rangeKind}`,
      `targetSolFraction=${targetSolFractionFinal} (SOL USD share of needSol+needUsdc at usdcPerSol=${usdcPerSol})`,
      `needSol=${needSol} needUsdc=${needUsdc}`,
      swapSide
        ? `swapSuggestion: ${swapSide} amount=${swapAmount}`
        : "swapSuggestion: none (wallet already covers need within fee reserve, or both sides short)",
    ],
  };
}
