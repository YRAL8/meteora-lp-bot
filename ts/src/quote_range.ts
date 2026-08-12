/**
 * Read-only probe: cost of wide ranges via quoteExtendPosition / quoteCreatePosition.
 * Human messages → stderr. Exactly one JSON object → stdout.
 * Never signs or sends; wallet not required.
 */
import { Connection, Keypair, PublicKey } from "@solana/web3.js";
import BN from "bn.js";
import DLMM, {
  DEFAULT_BIN_PER_POSITION,
  StrategyType,
  getExtendedPositionBinCount,
} from "@meteora-ag/dlmm";
import {
  DEFAULT_POOL,
  eprint,
  fail,
  flagStr,
  parseArgs,
  rpcHostForLog,
} from "./build_lib";

type Json = Record<string, unknown>;

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** Transient RPC errors — same idea as build_lib.withRpcRetry (not exported). */
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

/** half such that (1 + binStep/10000)^half - 1 ≈ pct/100 */
function pctToHalfWidth(pct: number, binStep: number): number {
  if (binStep <= 0) throw new Error("binStep must be > 0");
  if (!(pct > 0)) throw new Error("pct must be > 0");
  const numer = Math.log(1 + pct / 100);
  const denom = Math.log(1 + binStep / 10_000);
  const half = Math.round(numer / denom);
  return Math.max(1, half);
}

function halfToPct(half: number, binStep: number): number {
  return (Math.pow(1 + binStep / 10_000, half) - 1) * 100;
}

function decToNumber(v: { toNumber?: () => number } | number | string): number {
  if (typeof v === "number") return v;
  if (typeof v === "string") return Number(v);
  if (v && typeof v.toNumber === "function") return v.toNumber();
  return Number(v);
}

async function main(): Promise<void> {
  // Allow `node quote_range.js --pool …` without a verb.
  const raw = process.argv.slice(2);
  if (raw.length === 0 || raw[0].startsWith("--")) {
    raw.unshift("quote-range");
  }
  const { flags } = parseArgs(raw);

  const rpc =
    flagStr(flags, "rpc") ||
    process.env.SOLANA_DEVNET_RPC_URL ||
    process.env.SOLANA_RPC_URL ||
    "https://api.mainnet-beta.solana.com";
  const poolStr = flagStr(flags, "pool", DEFAULT_POOL)!;
  const percentsRaw = flagStr(flags, "percents", "0.25,0.5,1,2,3")!;
  const percents = percentsRaw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => Number(s));
  if (percents.some((p) => !Number.isFinite(p) || p <= 0)) {
    fail("quote-range", `bad --percents: ${percentsRaw}`, "parseArgs");
  }

  eprint(
    `quote-range rpc=${rpcHostForLog(rpc)} pool=${poolStr} percents=${percents.join(",")}`
  );

  const connection = new Connection(rpc, "confirmed");
  const pool = new PublicKey(poolStr);
  // Arbitrary pubkey — quotes do not require a funded wallet.
  const dummyOwner = Keypair.generate().publicKey;

  let dlmm: Awaited<ReturnType<typeof DLMM.create>>;
  try {
    dlmm = await withRpcRetry("DLMM.create", () =>
      DLMM.create(connection, pool)
    );
    await withRpcRetry("refetchStates", () => dlmm.refetchStates());
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    fail("quote-range", msg, "DLMM.create");
  }

  const binStep = Number(dlmm.lbPair.binStep);
  const activeId = Number(dlmm.lbPair.activeId);
  const defaultBins = DEFAULT_BIN_PER_POSITION.toNumber();
  eprint(
    `pool loaded binStep=${binStep} activeId=${activeId} DEFAULT_BIN_PER_POSITION=${defaultBins} owner(dummy)=${dummyOwner.toBase58()}`
  );

  // Largest half-width that still fits a simple (non-extended) position.
  let maxSimpleHalf = 0;
  for (let half = 1; half < defaultBins; half++) {
    const minB = activeId - half;
    const maxB = activeId + half;
    const ext = getExtendedPositionBinCount(new BN(minB), new BN(maxB));
    if (ext.isZero()) maxSimpleHalf = half;
    else break;
  }
  const maxSimplePct = halfToPct(maxSimpleHalf, binStep);
  eprint(
    `simple-position limit: half=${maxSimpleHalf} bins/side → ±${maxSimplePct.toFixed(4)}%`
  );

  const rows: Json[] = [];
  for (const pct of percents) {
    await sleep(400);
    const half = pctToHalfWidth(pct, binStep);
    const minBinId = activeId - half;
    const maxBinId = activeId + half;
    const width = maxBinId - minBinId + 1;
    const extendedBn = getExtendedPositionBinCount(
      new BN(minBinId),
      new BN(maxBinId)
    );
    const extendedBins = extendedBn.toNumber();
    const fitsSimple = extendedBins === 0;

    let positionExtendCostSol: number | null = null;
    let binArrayCostSol: number | null = null;
    if (!fitsSimple) {
      try {
        // Base = standard-width position starting at minBinId; expand upward.
        const baseMax = minBinId + defaultBins - 1;
        const q = await withRpcRetry("quoteExtendPosition", () =>
          dlmm.quoteExtendPosition(
            new BN(minBinId),
            new BN(baseMax),
            extendedBn
          )
        );
        positionExtendCostSol = decToNumber(q.positionExtendCost);
        binArrayCostSol = decToNumber(q.binArrayCost);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        eprint(`quoteExtendPosition failed pct=${pct}: ${msg}`);
      }
      await sleep(400);
    }

    let create: Json | null = null;
    try {
      const cq = await withRpcRetry("quoteCreatePosition", () =>
        dlmm.quoteCreatePosition({
          strategy: {
            minBinId,
            maxBinId,
            strategyType: StrategyType.Spot,
          },
        })
      );
      create = {
        positionCount: cq.positionCount,
        positionCostSol: cq.positionCost,
        positionReallocCostSol: cq.positionReallocCost,
        binArrayCostSol: cq.binArrayCost,
        binArraysCount: cq.binArraysCount,
        transactionCount: cq.transactionCount,
      };
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      eprint(`quoteCreatePosition failed pct=${pct}: ${msg}`);
    }

    const row: Json = {
      percent: pct,
      halfWidthBins: half,
      minBinId,
      maxBinId,
      widthBins: width,
      fitsSimple,
      extendedBinCount: extendedBins,
      extend: fitsSimple
        ? null
        : {
            positionExtendCostSol,
            binArrayCostSol,
          },
      createMulti: create,
      realizedPercent: halfToPct(half, binStep),
    };
    rows.push(row);
    eprint(
      `±${pct}% → half=${half} width=${width} fitsSimple=${fitsSimple}` +
        (fitsSimple
          ? ""
          : ` extendCost=${positionExtendCostSol} binArrays=${binArrayCostSol}`) +
        (create
          ? ` multiPos=${create.positionCount} multiCost=${create.positionCostSol}`
          : "")
    );
  }

  const out: Json = {
    ok: true,
    action: "quote-range",
    pool: poolStr,
    rpcHost: rpcHostForLog(rpc),
    binStep,
    activeId,
    defaultBinPerPosition: defaultBins,
    maxSimpleHalfWidthBins: maxSimpleHalf,
    maxSimplePercent: maxSimplePct,
    rows,
  };
  process.stdout.write(JSON.stringify(out) + "\n");
}

main().catch((err) => {
  const msg = err instanceof Error ? err.message : String(err);
  fail("quote-range", msg, "runtime");
});
