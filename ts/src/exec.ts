/**
 * Sign + send Meteora DLMM operations (devnet by default).
 * All dangerous logic lives here — cli.ts stays sterile.
 */
import {
  Connection,
  Keypair,
  PublicKey,
  VersionedTransaction,
} from "@solana/web3.js";
import {
  DEFAULT_POOL,
  cmdBuildAdd,
  cmdBuildClaimFees,
  cmdBuildClose,
  cmdBuildCloseEmpty,
  cmdBuildOpen,
  cmdBuildSwap,
  cmdBuildWithdraw,
  eprint,
  fail,
  flagStr,
  parseArgs,
  prepareTxForSend,
  requireFlag,
  rpcHostForLog,
} from "./build_lib";
import {
  appendJournalEntry,
  defaultJournalPath,
  hasUnresolved,
  isTransientRpcError,
  pollSignatureStatus,
  readJournal,
  resolveJournalOnStartup,
  unresolvedEntries,
  updateJournalBySignature,
  type JournalEntry,
} from "./journal";
import {
  assertSendAllowed,
  defaultRpcForNetwork,
  explorerTxUrl,
  parseNetwork,
  type Network,
} from "./network";
import { loadWalletKeypair } from "./wallet";

type Json = Record<string, unknown>;

type TxMeta = {
  index: number;
  kind: string;
  base64: string;
  signers: string[];
};

const EXEC_COMMANDS = [
  "exec-open",
  "exec-add",
  "exec-claim-fees",
  "exec-withdraw",
  "exec-close",
  "exec-close-empty",
  "exec-swap",
] as const;

type ExecCommand = (typeof EXEC_COMMANDS)[number];

function isExecCommand(cmd: string): cmd is ExecCommand {
  return (EXEC_COMMANDS as readonly string[]).includes(cmd);
}

function buildActionForExec(cmd: ExecCommand): string {
  return cmd.replace(/^exec-/, "build-");
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function partialFillExtra(
  sends: Json[],
  totalTxs: number
): Record<string, unknown> {
  const confirmed = sends.filter((s) => s.status === "confirmed").length;
  return {
    partialFill: confirmed > 0 && confirmed < totalTxs,
    confirmedCount: confirmed,
    totalTxs,
  };
}

function partialFillMessage(
  index: number,
  total: number,
  confirmed: number,
  detail: string
): string {
  if (confirmed <= 0) return detail;
  return (
    `PARTIAL FILL: ${confirmed}/${total} txs confirmed before failure at tx[${index}]. ` +
    `${detail} Position may be only partly funded — check on-chain /status; ` +
    "do not treat this as a full deposit. Close or top up consciously."
  );
}

async function runBuild(
  cmd: ExecCommand,
  connection: Connection,
  pool: PublicKey,
  flags: Record<string, string | boolean>,
  ownerPubkey: string,
  positionKeypair?: Keypair
): Promise<Json> {
  flags = { ...flags, owner: ownerPubkey };
  if (cmd === "exec-open") {
    if (flags["position-pubkey"]) {
      fail(
        cmd,
        "exec-open must not use --position-pubkey; position key is ephemeral in-process",
        "parseArgs"
      );
    }
    if (!positionKeypair) {
      fail(cmd, "internal: position keypair missing for exec-open", "runtime");
    }
    return cmdBuildOpen(connection, pool, flags, {
      positionKeypair,
    });
  }
  switch (cmd) {
    case "exec-add":
      return cmdBuildAdd(connection, pool, flags);
    case "exec-claim-fees":
      return cmdBuildClaimFees(connection, pool, flags);
    case "exec-withdraw":
      return cmdBuildWithdraw(connection, pool, flags);
    case "exec-close":
      return cmdBuildClose(connection, pool, flags);
    case "exec-close-empty":
      return cmdBuildCloseEmpty(connection, pool, flags);
    case "exec-swap":
      return cmdBuildSwap(connection, pool, flags);
    default:
      fail(cmd, `unknown exec command: ${cmd}`, "parseArgs");
  }
}

async function sendBuildResult(
  connection: Connection,
  network: Network,
  action: string,
  build: Json,
  wallet: Keypair,
  journalPath: string,
  positionKeypair?: Keypair,
  opts?: { crashAfterSend?: boolean }
): Promise<Json> {
  const txs = (build.txs as TxMeta[]) || [];
  const params = (build.params as Record<string, unknown>) || {};
  const signatures: string[] = [];
  const sends: Json[] = [];
  const available: Keypair[] = [wallet];
  if (positionKeypair) available.push(positionKeypair);

  for (const txMeta of txs) {
    let vtx = VersionedTransaction.deserialize(
      Buffer.from(txMeta.base64, "base64")
    );

    let signature: string;
    try {
      const prepared = await prepareTxForSend(
        vtx,
        txMeta.index,
        available,
        async () => {
          const { blockhash } = await connection.getLatestBlockhash("confirmed");
          return blockhash;
        }
      );
      vtx = prepared.vtx;
      signature = prepared.signature;
      if (prepared.refreshed) {
        eprint(
          `tx[${txMeta.index}] re-signed with fresh blockhash ` +
            `(signers=${prepared.signers.join(",")})`
        );
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      const confirmed = sends.filter((s) => s.status === "confirmed").length;
      fail(
        action,
        partialFillMessage(
          txMeta.index,
          txs.length,
          confirmed,
          `sign/prepare failed tx[${txMeta.index}]: ${msg}`
        ),
        "sign",
        {
          network,
          signatures,
          sends,
          params,
          ...partialFillExtra(sends, txs.length),
        }
      );
    }

    // Journal AFTER final sign so the recorded signature is the one on the wire.
    const pending: JournalEntry = {
      ts: new Date().toISOString(),
      action,
      network,
      params: { ...params, txIndex: txMeta.index },
      signature,
      status: "pending",
      slot: null,
      error: null,
      txIndex: txMeta.index,
    };
    appendJournalEntry(journalPath, pending);

    eprint(`sending tx[${txMeta.index}] signature=${signature}`);
    try {
      await connection.sendRawTransaction(vtx.serialize(), {
        skipPreflight: true,
        maxRetries: 3,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      // Client send error ≠ proof the cluster rejected the tx (skipPreflight).
      updateJournalBySignature(journalPath, signature, {
        status: "unknown",
        error: `send exception (may still land): ${msg}`,
      });
      const sendsSoFar = [
        ...sends,
        {
          index: txMeta.index,
          signature,
          status: "unknown",
          slot: null,
          explorer: explorerTxUrl(signature, network),
          error: msg,
        },
      ];
      const sigsSoFar = [...signatures, signature];
      const confirmed = sends.filter((s) => s.status === "confirmed").length;
      fail(
        action,
        partialFillMessage(
          txMeta.index,
          txs.length,
          confirmed,
          `send failed tx[${txMeta.index}]: ${msg}`
        ),
        "send",
        {
          confirmationUnknown: true,
          network,
          signatures: sigsSoFar,
          sends: sendsSoFar,
          params,
          ...partialFillExtra(sendsSoFar, txs.length),
        }
      );
    }

    if (opts?.crashAfterSend) {
      eprint("crashAfterSend: exiting before confirmation (test hook)");
      process.exit(42);
    }

    const { outcome, slot, error } = await pollSignatureStatus(
      connection,
      signature
    );
    const status =
      outcome === "confirmed"
        ? "confirmed"
        : outcome === "failed"
          ? "failed"
          : "unknown";
    updateJournalBySignature(journalPath, signature, {
      status,
      slot,
      error,
    });

    signatures.push(signature);
    sends.push({
      index: txMeta.index,
      signature,
      status,
      slot,
      explorer: explorerTxUrl(signature, network),
      error,
    });

    if (status === "failed") {
      const confirmed = sends.filter((s) => s.status === "confirmed").length;
      fail(
        action,
        partialFillMessage(
          txMeta.index,
          txs.length,
          confirmed,
          `tx[${txMeta.index}] failed on-chain: ${error}`
        ),
        "confirm",
        {
          network,
          signatures,
          sends,
          params,
          ...partialFillExtra(sends, txs.length),
        }
      );
    }
    if (status === "unknown") {
      eprint(
        `tx[${txMeta.index}] confirmation unknown — failing this exec; ` +
          "journal left as unknown; do not retry blindly"
      );
      const confirmed = sends.filter((s) => s.status === "confirmed").length;
      fail(
        action,
        partialFillMessage(
          txMeta.index,
          txs.length,
          confirmed,
          `tx[${txMeta.index}] confirmation unknown — do not retry blindly`
        ),
        "confirm-unknown",
        {
          confirmationUnknown: true,
          network,
          signatures,
          sends,
          params,
          ...partialFillExtra(sends, txs.length),
        }
      );
    }
    await sleep(500);
  }

  return {
    ok: true,
    action,
    network,
    owner: wallet.publicKey.toBase58(),
    pool: build.pool,
    params,
    sent: true,
    signatures,
    sends,
    build,
  };
}

async function main(): Promise<void> {
  const { cmd, flags } = parseArgs(process.argv.slice(2));

  // Chat-reachable unlock path: re-poll unresolved journal entries (no send).
  if (cmd === "resolve-journal") {
    const network = parseNetwork(flags, cmd);
    const rpc =
      flagStr(flags, "rpc") || defaultRpcForNetwork(network);
    const connection = new Connection(rpc, "confirmed");
    const journalPath = defaultJournalPath();
    const pollMs = Number(flagStr(flags, "poll-ms") || "1500");
    // Default 25s per sig; wall budget shared so N stuck sigs still finish.
    const perSigTimeout = Number(flagStr(flags, "timeout-ms") || "25000");
    const wallMs = Number(flagStr(flags, "wall-ms") || "170000");
    eprint(`resolve-journal rpc=${rpcHostForLog(rpc)} journal=${journalPath}`);
    const before = unresolvedEntries(readJournal(journalPath));
    const wallStart = Date.now();
    let rpcDownHint: string | null = null;
    for (const e of before) {
      const remaining = wallMs - (Date.now() - wallStart);
      if (remaining < 2_000) {
        eprint("resolve-journal: wall budget exhausted — leaving rest unresolved");
        break;
      }
      const thisTimeout = Math.min(perSigTimeout, remaining);
      const { outcome, slot, error } = await pollSignatureStatus(
        connection,
        e.signature,
        thisTimeout,
        pollMs
      );
      if (
        outcome === "unknown" &&
        error &&
        isTransientRpcError(error)
      ) {
        rpcDownHint =
          "RPC unreachable or flaky — retry /status journal when the node is up, " +
          "or check explorer and /status journal-forget <sig> confirm if the tx is final.";
      }
      if (outcome === "confirmed") {
        updateJournalBySignature(journalPath, e.signature, {
          status: "confirmed",
          slot,
          error: null,
        });
      } else if (outcome === "failed") {
        updateJournalBySignature(journalPath, e.signature, {
          status: "failed",
          slot,
          error,
        });
      }
    }
    const after = unresolvedEntries(readJournal(journalPath));
    process.stdout.write(
      JSON.stringify({
        ok: true,
        action: "resolve-journal",
        network,
        journalPath,
        before: before.map((e) => ({
          signature: e.signature,
          status: e.status,
          action: e.action,
        })),
        stillUnresolved: after.map((e) => ({
          signature: e.signature,
          status: e.status,
          action: e.action,
          explorer: explorerTxUrl(e.signature, network),
        })),
        cleared: before.length - after.length,
        rpcHint: rpcDownHint,
      }) + "\n"
    );
    return;
  }

  if (!isExecCommand(cmd)) {
    fail(cmd, `unknown command: ${cmd}`, "parseArgs");
  }

  const network = parseNetwork(flags, cmd);
  const send = flags["send"] === true;
  const dryRun = !send;
  const forceIgnoreJournal = flags["force-ignore-journal"] === true;
  const crashAfterSend = flags["crash-after-send"] === true;
  try {
    assertSendAllowed(network, send, cmd);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    fail(cmd, msg, "network");
  }

  const rpc =
    flagStr(flags, "rpc") || defaultRpcForNetwork(network);
  const poolStr = flagStr(flags, "pool", DEFAULT_POOL)!;
  const connection = new Connection(rpc, "confirmed");
  const pool = new PublicKey(poolStr);
  const journalPath = defaultJournalPath();

  const { keypair: wallet, pubkey } = loadWalletKeypair(
    flagStr(flags, "wallet")
  );
  eprint(
    `cmd=${cmd} network=${network} rpc=${rpcHostForLog(rpc)} pool=${poolStr} wallet=${pubkey} ` +
      `mode=${dryRun ? "dry-run" : "send"} journal=${journalPath}`
  );

  if (!forceIgnoreJournal) {
    await resolveJournalOnStartup(connection, journalPath);
    const entries = readJournal(journalPath);
    if (hasUnresolved(entries)) {
      const open = unresolvedEntries(entries);
      const sigs = open.map((e) => `${e.signature}(${e.status})`).join(", ");
      fail(
        cmd,
        `journal has unresolved entries: ${sigs}. Resolve manually or use --force-ignore-journal`,
        "journal"
      );
    }
  } else {
    eprint("warning: --force-ignore-journal — skipping journal block check");
    await resolveJournalOnStartup(connection, journalPath);
  }

  let positionKeypair: Keypair | undefined;
  if (cmd === "exec-open" && send) {
    positionKeypair = Keypair.generate();
    eprint(
      `exec-open: ephemeral position pubkey=${positionKeypair.publicKey.toBase58()}`
    );
  } else if (cmd === "exec-open" && dryRun) {
    positionKeypair = Keypair.generate();
  }

  const buildAction = buildActionForExec(cmd);
  let build: Json;
  try {
    build = await runBuild(
      cmd,
      connection,
      pool,
      flags,
      pubkey,
      positionKeypair
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    fail(cmd, msg, "build");
  }

  if (build.ok === false) {
    process.stdout.write(JSON.stringify({ ...build, action: cmd }) + "\n");
    process.exit(1);
  }

  if (dryRun) {
    const out: Json = {
      ...build,
      action: cmd,
      network,
      dryRun: true,
      wallet: pubkey,
      note: "simulation ok; pass --send to submit (still requires successful simulate)",
    };
    process.stdout.write(JSON.stringify(out) + "\n");
    return;
  }

  const out = await sendBuildResult(
    connection,
    network,
    cmd,
    { ...build, action: buildAction },
    wallet,
    journalPath,
    positionKeypair,
    { crashAfterSend }
  );
  process.stdout.write(JSON.stringify({ ...out, action: cmd }) + "\n");
}

main().catch((err) => {
  const msg = err instanceof Error ? err.message : String(err);
  fail("exec", msg, "runtime");
});
