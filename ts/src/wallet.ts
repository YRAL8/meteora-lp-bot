import fs from "fs";
import os from "os";
import path from "path";
import { Keypair } from "@solana/web3.js";
import { eprint } from "./build_lib";

function expandHome(p: string): string {
  if (p.startsWith("~/")) {
    return path.join(os.homedir(), p.slice(2));
  }
  return p;
}

export function defaultWalletPath(): string {
  return expandHome(
    process.env.WALLET_KEYPAIR_PATH ||
      "~/.config/solana/meteora-devnet.json"
  );
}

/** Load wallet keypair from disk. Secret never logged or returned as JSON. */
export function loadWalletKeypair(
  keypairPath?: string
): { keypair: Keypair; pubkey: string; path: string } {
  const resolved = path.resolve(keypairPath || defaultWalletPath());
  if (!fs.existsSync(resolved)) {
    throw new Error(`wallet keypair file not found: ${resolved}`);
  }
  const stat = fs.statSync(resolved);
  const mode = stat.mode & 0o777;
  if (mode & 0o077) {
    eprint(
      `warning: wallet keypair ${resolved} has permissive mode ${mode.toString(8)}; ` +
        "recommended 600 (owner read/write only)"
    );
  }
  const raw = fs.readFileSync(resolved, "utf8");
  let secret: number[];
  try {
    secret = JSON.parse(raw) as number[];
  } catch {
    throw new Error(`wallet keypair ${resolved} is not valid JSON array`);
  }
  if (!Array.isArray(secret) || secret.length < 64) {
    throw new Error(`wallet keypair ${resolved} must be a 64-byte secret array`);
  }
  const keypair = Keypair.fromSecretKey(Uint8Array.from(secret));
  return { keypair, pubkey: keypair.publicKey.toBase58(), path: resolved };
}
