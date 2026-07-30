/**
 * Dry-run TX builder for Meteora DLMM.
 * Human messages → stderr. Exactly one JSON object → stdout.
 * Never signs or sends; only connection.simulateTransaction.
 */
import { Connection, PublicKey } from "@solana/web3.js";
import {
  DEFAULT_POOL,
  DEFAULT_RPC,
  cmdBalances,
  cmdBuildAdd,
  cmdBuildClaimFees,
  cmdBuildClose,
  cmdBuildOpen,
  cmdBuildSwap,
  cmdBuildWithdraw,
  cmdListPositions,
  cmdPoolInfo,
  cmdSuggestAmounts,
  eprint,
  fail,
  flagStr,
  parseArgs,
  requireFlag,
  rpcHostForLog,
} from "./build_lib";

type Json = Record<string, unknown>;

async function main(): Promise<void> {
  const { cmd, flags } = parseArgs(process.argv.slice(2));
  const rpc = flagStr(flags, "rpc", DEFAULT_RPC)!;
  const poolStr = flagStr(flags, "pool", DEFAULT_POOL)!;
  const connection = new Connection(rpc, "confirmed");
  const pool = new PublicKey(poolStr);

  eprint(`cmd=${cmd} rpc=${rpcHostForLog(rpc)} pool=${poolStr}`);

  try {
    let out: Json;
    switch (cmd) {
      case "pool-info":
        out = await cmdPoolInfo(connection, pool);
        break;
      case "list-positions":
        out = await cmdListPositions(
          connection,
          pool,
          requireFlag(flags, "owner", cmd)
        );
        break;
      case "balances":
        out = await cmdBalances(
          connection,
          pool,
          requireFlag(flags, "owner", cmd)
        );
        break;
      case "suggest-amounts":
        out = await cmdSuggestAmounts(connection, pool, flags);
        break;
      case "build-open":
        out = await cmdBuildOpen(connection, pool, flags);
        break;
      case "build-add":
        out = await cmdBuildAdd(connection, pool, flags);
        break;
      case "build-claim-fees":
        out = await cmdBuildClaimFees(connection, pool, flags);
        break;
      case "build-withdraw":
        out = await cmdBuildWithdraw(connection, pool, flags);
        break;
      case "build-close":
        out = await cmdBuildClose(connection, pool, flags);
        break;
      case "build-swap":
        out = await cmdBuildSwap(connection, pool, flags);
        break;
      default:
        fail(cmd, `unknown command: ${cmd}`, "parseArgs");
    }
    process.stdout.write(JSON.stringify(out) + "\n");
    if (out.ok === false) process.exit(1);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    fail(cmd, msg, "runtime");
  }
}

main();
