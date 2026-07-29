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
  requireFlag,
} from "./build_lib";
import {
  appendJournalEntry,
  defaultJournalPath,
  hasUnresolved,
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

// bs58 is a transitive dep of @solana/web3.js (signature encoding only).
// eslint-disable-next-line @typescript-eslint/no-require-imports
const bs58 = require("bs58") as { encode: (b: Uint8Array) => string };

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

function signatureFromV0(vtx: VersionedTransaction): string {
  const sigBytes = vtx.signatures[0];
  if (!sigBytes || sigBytes.every((b) => b === 0)) {
    throw new Error("transaction not signed");
  }
  return bs58.encode(sigBytes);
}

function pickSigners(
  requiredPubkeys: string[],
  wallet: Keypair,
  positionKeypair?: Keypair
): Keypair[] {
  const map = new Map<string, Keypair>();
  map.set(wallet.publicKey.toBase58(), wallet);
  if (positionKeypair) {
    map.set(positionKeypair.publicKey.toBase58(), positionKeypair);
  }
  const out: Keypair[] = [];
  for (const pk of requiredPubkeys) {
    const kp = map.get(pk);
    if (!kp) {
      throw new Error(`missing signer keypair for ${pk}`);
    }
    out.push(kp);
  }
  return out;
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

  for (const txMeta of txs) {
    const vtx = VersionedTransaction.deserialize(
      Buffer.from(txMeta.base64, "base64")
    );
    const signerKps = pickSigners(txMeta.signers, wallet, positionKeypair);
    vtx.sign(signerKps);
    const signature = signatureFromV0(vtx);

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
      updateJournalBySignature(journalPath, signature, {
        status: "failed",
        error: msg,
      });
      fail(action, `send failed tx[${txMeta.index}]: ${msg}`, "send");
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
      fail(
        action,
        `tx[${txMeta.index}] failed on-chain: ${error}`,
        "confirm"
      );
    }
    if (status === "unknown") {
      eprint(
        `warning: tx[${txMeta.index}] confirmation unknown — journal left as unknown; ` +
          "do not retry blindly"
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
    `cmd=${cmd} network=${network} rpc=${rpc} pool=${poolStr} wallet=${pubkey} ` +
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
