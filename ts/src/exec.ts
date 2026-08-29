/**
 * Sign + send Meteora DLMM operations (devnet by default).
 * All dangerous logic lives here — cli.ts stays sterile.
 *
 * Send journal is owned by Python (@@JOURNAL markers on stderr). This process
 * does not read or write the send-journal or its lock file.
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
  confirmWithRebroadcast,
  fail,
  flagStr,
  parseArgs,
  prepareTxForSend,
  rpcHostForLog,
} from "./build_lib";
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

/** Map tx meta / action to short journal kind for @@JOURNAL markers. */
function journalKind(txKind: string, action: string): string {
  const k = (txKind || "").toLowerCase();
  if (
    k === "open" ||
    k === "close" ||
    k === "swap" ||
    k === "add" ||
    k === "withdraw" ||
    k === "claim"
  ) {
    return k;
  }
  const fromAction = action.replace(/^exec-/, "").replace(/^build-/, "");
  if (fromAction === "close-empty") return "close";
  if (fromAction === "claim-fees") return "claim";
  if (
    fromAction === "open" ||
    fromAction === "close" ||
    fromAction === "swap" ||
    fromAction === "add" ||
    fromAction === "withdraw"
  ) {
    return fromAction;
  }
  return "open";
}

const JOURNAL_HANDSHAKE_MS = Number(
  process.env.METEORA_JOURNAL_HANDSHAKE_MS || "15000"
);

/** Emit machine marker for Python journal owner (parent acks on stdin). */
function emitJournalSending(
  index: number,
  signature: string,
  kind: string
): void {
  const line =
    `@@JOURNAL ${JSON.stringify({
      stage: "sending",
      index,
      signature,
      kind,
    })}\n`;
  process.stderr.write(line);
}

/**
 * Wait for parent journal ack on stdin.
 * Returns true only for exact ``@@JOURNAL-OK <signature>``.
 * FAIL / mismatch / timeout / stdin EOF → false (must not send).
 */
function awaitJournalHandshake(signature: string): Promise<boolean> {
  const timeoutMs =
    Number.isFinite(JOURNAL_HANDSHAKE_MS) && JOURNAL_HANDSHAKE_MS > 0
      ? JOURNAL_HANDSHAKE_MS
      : 15_000;
  return new Promise((resolve) => {
    let buf = "";
    let settled = false;
    const finish = (ok: boolean) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      process.stdin.removeListener("data", onData);
      process.stdin.removeListener("end", onEnd);
      process.stdin.removeListener("error", onEnd);
      // resume() to read the ack leaves stdin holding the event loop open, so a
      // finished run would never exit: the devnet stand failed 5/5 on a 300s
      // parent timeout while every transaction had already confirmed. pause()
      // alone is enough (measured on a real parent-held pipe); unref() is kept
      // as belt and braces, with ref() restored before the next wait.
      try {
        process.stdin.pause();
        if (typeof process.stdin.unref === "function") {
          process.stdin.unref();
        }
      } catch {
        /* ignore */
      }
      resolve(ok);
    };
    const timer = setTimeout(() => finish(false), timeoutMs);
    const onData = (chunk: Buffer | string) => {
      buf += typeof chunk === "string" ? chunk : chunk.toString("utf8");
      const nl = buf.indexOf("\n");
      if (nl < 0) return;
      const line = buf.slice(0, nl).trim();
      if (line === `@@JOURNAL-OK ${signature}`) {
        finish(true);
        return;
      }
      // @@JOURNAL-FAIL, wrong signature, or garbage — refuse send.
      finish(false);
    };
    const onEnd = () => finish(false);
    process.stdin.on("data", onData);
    process.stdin.on("end", onEnd);
    process.stdin.on("error", onEnd);
    try {
      if (typeof process.stdin.ref === "function") {
        process.stdin.ref();
      }
    } catch {
      /* ignore */
    }
    if (process.stdin.isPaused()) process.stdin.resume();
  });
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
    let lastValidBlockHeight: number;
    try {
      const prepared = await prepareTxForSend(
        vtx,
        txMeta.index,
        available,
        async () => connection.getLatestBlockhash("confirmed")
      );
      vtx = prepared.vtx;
      signature = prepared.signature;
      lastValidBlockHeight = prepared.lastValidBlockHeight;
      eprint(
        `tx[${txMeta.index}] re-signed with fresh blockhash ` +
          `(lastValidBlockHeight=${lastValidBlockHeight}, ` +
          `signers=${prepared.signers.join(",")})`
      );
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

    // Python owns the journal: marker + stdin ack before bytes hit the wire.
    emitJournalSending(
      txMeta.index,
      signature,
      journalKind(txMeta.kind, action)
    );
    const journalOk = await awaitJournalHandshake(signature);
    if (!journalOk) {
      const sendsSoFar = [
        ...sends,
        {
          index: txMeta.index,
          signature,
          status: "not_sent",
          sent: false,
          slot: null,
          explorer: explorerTxUrl(signature, network),
          error: "journal-refused",
        },
      ];
      const confirmed = sends.filter((s) => s.status === "confirmed").length;
      fail(
        action,
        partialFillMessage(
          txMeta.index,
          txs.length,
          confirmed,
          `journal refused send for tx[${txMeta.index}] — not sent`
        ),
        "journal",
        {
          confirmationUnknown: false,
          network,
          signatures,
          sends: sendsSoFar,
          params,
          ...partialFillExtra(sendsSoFar, txs.length),
        }
      );
    }

    eprint(`sending tx[${txMeta.index}] signature=${signature}`);
    const rawTx = vtx.serialize();
    try {
      await connection.sendRawTransaction(rawTx, {
        skipPreflight: true,
        maxRetries: 3,
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      // Client send error ≠ proof the cluster rejected the tx (skipPreflight).
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

    const { outcome, slot, error } = await confirmWithRebroadcast(
      connection,
      rawTx,
      signature,
      lastValidBlockHeight
    );
    const status = outcome;

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
    if (status === "dropped") {
      eprint(
        `tx[${txMeta.index}] dropped — blockhash expired and the signature ` +
          "never reached the cluster; nothing was executed, retrying is safe"
      );
      const confirmed = sends.filter((s) => s.status === "confirmed").length;
      fail(
        action,
        partialFillMessage(
          txMeta.index,
          txs.length,
          confirmed,
          `tx[${txMeta.index}] dropped before inclusion (blockhash expired) — ` +
            "nothing was executed; safe to retry"
        ),
        "confirm-dropped",
        {
          confirmationUnknown: false,
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
          "do not retry blindly (Python journal left as unknown after sync)"
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

async function runHandshakeSelfTest(flags: Record<string, string | boolean>): Promise<void> {
  /** Offline lifecycle probe: one journal handshake, no network/wallet/send. */
  const signature = flagStr(flags, "signature");
  if (!signature) {
    fail("handshake-self-test", "missing --signature", "parseArgs");
  }
  emitJournalSending(0, signature, "open");
  const journalOk = await awaitJournalHandshake(signature);
  process.stdout.write(
    JSON.stringify({
      ok: journalOk,
      action: "handshake-self-test",
      signature,
      journalOk,
    }) + "\n"
  );
  // Natural return — must not process.exit(); hanging stdin would hide here.
}

async function main(): Promise<void> {
  const { cmd, flags } = parseArgs(process.argv.slice(2));

  if (cmd === "handshake-self-test") {
    await runHandshakeSelfTest(flags);
    return;
  }

  if (!isExecCommand(cmd)) {
    fail(cmd, `unknown command: ${cmd}`, "parseArgs");
  }

  const network = parseNetwork(flags, cmd);
  const send = flags["send"] === true;
  const dryRun = !send;
  const crashAfterSend = flags["crash-after-send"] === true;
  try {
    assertSendAllowed(network, send, cmd);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    fail(cmd, msg, "network");
  }

  const rpc = flagStr(flags, "rpc") || defaultRpcForNetwork(network);
  const poolStr = flagStr(flags, "pool", DEFAULT_POOL)!;
  const connection = new Connection(rpc, "confirmed");
  const pool = new PublicKey(poolStr);

  const { keypair: wallet, pubkey } = loadWalletKeypair(
    flagStr(flags, "wallet")
  );
  eprint(
    `cmd=${cmd} network=${network} rpc=${rpcHostForLog(rpc)} pool=${poolStr} wallet=${pubkey} ` +
      `mode=${dryRun ? "dry-run" : "send"}`
  );

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
    positionKeypair,
    { crashAfterSend }
  );
  process.stdout.write(JSON.stringify({ ...out, action: cmd }) + "\n");
}

main().catch((err) => {
  const msg = err instanceof Error ? err.message : String(err);
  fail("exec", msg, "runtime");
});
