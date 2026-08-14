/**
 * Build-only probe: how wide an open still fits in ONE transaction.
 * Never signs or sends. Simulation is skipped — we only count txs the SDK returned.
 *
 * Human messages → stderr (RPC host without query). One JSON object → stdout.
 */
import { Connection, Keypair, PublicKey, Transaction } from "@solana/web3.js";
import BN from "bn.js";
import DLMM, {
  DEFAULT_BIN_PER_POSITION,
  MAX_BIN_LENGTH_ALLOWED_IN_ONE_TX,
  POSITION_MAX_LENGTH,
  StrategyType,
  calculatePositionSize,
  getBinArrayKeysCoverage,
} from "@meteora-ag/dlmm";
import {
  DEFAULT_POOL,
  DEFAULT_RPC,
  eprint,
  fail,
  flagNum,
  flagStr,
  parseArgs,
  rpcHostForLog,
} from "./build_lib";

type Json = Record<string, unknown>;

const ACCOUNT_DISCRIMINATOR_BYTES = 8;
const DLMM_PROGRAM_ID = new PublicKey(
  "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
);

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function withRpcRetry<T>(label: string, fn: () => Promise<T>): Promise<T> {
  let last: unknown;
  for (let i = 0; i < 6; i++) {
    try {
      return await fn();
    } catch (err) {
      last = err;
      const msg = err instanceof Error ? err.message : String(err);
      const low = msg.toLowerCase();
      const transient =
        msg.includes("429") ||
        low.includes("too many requests") ||
        low.includes("rate limit") ||
        low.includes("fetch failed") ||
        low.includes("socket hang up") ||
        low.includes("econnreset") ||
        low.includes("econnrefused");
      if (!transient || i === 5) break;
      const ms = Math.min(8000, 400 * 2 ** i + Math.floor(Math.random() * 200));
      eprint(`RPC retry (${label}), sleep ${ms}ms: ${msg}`);
      await sleep(ms);
    }
  }
  throw last instanceof Error ? last : new Error(String(last));
}

function toTxArray(tx: Transaction | Transaction[]): Transaction[] {
  return Array.isArray(tx) ? tx : [tx];
}

function uiToAmount(ui: number, decimals: number): BN {
  const s = ui.toFixed(decimals);
  const [whole, frac = ""] = s.split(".");
  const padded = (frac + "0".repeat(decimals)).slice(0, decimals);
  return new BN(whole + padded);
}

function pctFromHalf(half: number, binStep: number): number {
  return (Math.pow(1 + binStep / 10_000, half) - 1) * 100;
}

function parseWidthList(raw: string | undefined): number[] {
  if (!raw) {
    // Coarse ladder from the SDK 26-bin chunk up past DEFAULT 70.
    return [26, 35, 50, 69, 70, 80, 91, 105, 140, 175, 210, 280];
  }
  return raw
    .split(",")
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isFinite(n) && n >= 1)
    .map((n) => Math.floor(n));
}

async function probeOne(
  connection: Connection,
  dlmm: DLMM,
  owner: PublicKey,
  width: number
): Promise<Json> {
  const activeId = dlmm.lbPair.activeId;
  const binStep = dlmm.lbPair.binStep;
  const halfDown = Math.floor((width - 1) / 2);
  const minBinId = activeId - halfDown;
  const maxBinId = minBinId + width - 1;
  const halfUp = maxBinId - activeId;
  const pct = pctFromHalf(Math.max(halfDown, halfUp), binStep);

  const solIsX = dlmm.tokenX.publicKey.toBase58() ===
    "So11111111111111111111111111111111111111112";
  const solDec = solIsX ? dlmm.tokenX.mint.decimals : dlmm.tokenY.mint.decimals;
  const usdcDec = solIsX ? dlmm.tokenY.mint.decimals : dlmm.tokenX.mint.decimals;
  const solRaw = uiToAmount(0.001, solDec);
  const usdcRaw = uiToAmount(0.1, usdcDec);
  const totalXAmount = solIsX ? solRaw : usdcRaw;
  const totalYAmount = solIsX ? usdcRaw : solRaw;

  const positionPubKey = Keypair.generate().publicKey;
  let txCount: number | null = null;
  let error: string | null = null;
  let ixCount: number | null = null;
  try {
    const built = await withRpcRetry("initializePositionAndAddLiquidityByStrategy", () =>
      dlmm.initializePositionAndAddLiquidityByStrategy({
        positionPubKey,
        totalXAmount,
        totalYAmount,
        strategy: {
          minBinId,
          maxBinId,
          strategyType: StrategyType.Spot,
        },
        user: owner,
        slippage: 1,
      })
    );
    const txs = toTxArray(built);
    txCount = txs.length;
    ixCount = txs.reduce((n, tx) => n + tx.instructions.length, 0);
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  const sdkSize = calculatePositionSize(new BN(width)).toNumber();
  const onChainSize = sdkSize + ACCOUNT_DISCRIMINATOR_BYTES;
  const rentLamports = await withRpcRetry("rent", () =>
    connection.getMinimumBalanceForRentExemption(onChainSize)
  );

  const keys = getBinArrayKeysCoverage(
    new BN(minBinId),
    new BN(maxBinId),
    dlmm.pubkey,
    DLMM_PROGRAM_ID
  );
  const infos = await withRpcRetry("binArrayAccounts", () =>
    connection.getMultipleAccountsInfo(keys)
  );
  const binArrayTotal = keys.length;
  const binArrayNew = infos.filter((a) => a === null).length;

  return {
    width,
    halfDown,
    halfUp,
    minBinId,
    maxBinId,
    pct,
    txCount,
    ixCount,
    error,
    positionRentLamports: rentLamports,
    positionRentSol: rentLamports / 1e9,
    onChainSizeBytes: onChainSize,
    sdkSizeBytes: sdkSize,
    binArrayTotal,
    binArrayNew,
  };
}

async function main(): Promise<void> {
  const { flags } = parseArgs(["width-probe", ...process.argv.slice(2)]);
  const rpc = flagStr(flags, "rpc", DEFAULT_RPC)!;
  const poolStr = flagStr(flags, "pool", DEFAULT_POOL)!;
  eprint(`cmd=width-probe rpc=${rpcHostForLog(rpc)} pool=${poolStr}`);

  const connection = new Connection(rpc, "confirmed");
  const pool = new PublicKey(poolStr);
  const dlmm = await withRpcRetry("DLMM.create", () =>
    DLMM.create(connection, pool)
  );
  await withRpcRetry("refetchStates", () => dlmm.refetchStates());

  const owner = Keypair.generate().publicKey;
  const widths = parseWidthList(flagStr(flags, "widths"));
  const rows: Json[] = [];
  for (const w of widths) {
    eprint(`build width=${w} (no sign, no send)`);
    const row = await probeOne(connection, dlmm, owner, w);
    rows.push(row);
    eprint(
      `  txs=${row.txCount} rentSol=${row.positionRentSol} ` +
        `binArray new/total=${row.binArrayNew}/${row.binArrayTotal}` +
        (row.error ? ` error=${String(row.error).slice(0, 120)}` : "")
    );
  }

  const oneTx = rows.filter((r) => r.txCount === 1 && !r.error);
  const multi = rows.filter(
    (r) => typeof r.txCount === "number" && (r.txCount as number) > 1
  );
  const failed = rows.filter((r) => r.error);
  const maxOne = oneTx.length
    ? oneTx.reduce((a, b) => ((a.width as number) > (b.width as number) ? a : b))
    : null;
  const firstMulti = multi.length
    ? multi.reduce((a, b) => ((a.width as number) < (b.width as number) ? a : b))
    : null;

  const out: Json = {
    ok: true,
    action: "width-probe",
    pool: poolStr,
    rpcHost: rpcHostForLog(rpc),
    activeId: dlmm.lbPair.activeId,
    binStep: dlmm.lbPair.binStep,
    sdk: {
      POSITION_MAX_LENGTH: POSITION_MAX_LENGTH.toNumber(),
      DEFAULT_BIN_PER_POSITION: DEFAULT_BIN_PER_POSITION.toNumber(),
      MAX_BIN_LENGTH_ALLOWED_IN_ONE_TX: MAX_BIN_LENGTH_ALLOWED_IN_ONE_TX,
    },
    method: "initializePositionAndAddLiquidityByStrategy",
    signed: false,
    sent: false,
    simulated: false,
    rows,
    maxWidthOneTx: maxOne ? maxOne.width : null,
    firstWidthMultiTx: firstMulti ? firstMulti.width : null,
    firstWidthError: failed.length ? failed[0].width : null,
  };
  process.stdout.write(JSON.stringify(out) + "\n");
}

main().catch((err) => {
  const msg = err instanceof Error ? err.message : String(err);
  fail("width-probe", msg, "runtime");
});
