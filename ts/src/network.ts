import { fail } from "./build_lib";

export type Network = "devnet" | "mainnet";

export function parseNetwork(
  flags: Record<string, string | boolean>,
  action: string
): Network {
  const raw = flags["network"];
  const n =
    raw === undefined || raw === true ? "devnet" : String(raw).toLowerCase();
  if (n !== "devnet" && n !== "mainnet") {
    fail(action, `--network must be devnet or mainnet, got ${n}`, "parseArgs");
  }
  return n;
}

export function defaultRpcForNetwork(network: Network): string {
  if (network === "devnet") {
    return process.env.SOLANA_DEVNET_RPC_URL || "https://api.devnet.solana.com";
  }
  return process.env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com";
}

/** Mainnet send requires both --network mainnet and METEORA_ALLOW_MAINNET=1. */
export function assertSendAllowed(
  network: Network,
  send: boolean,
  action: string
): void {
  if (!send) return;
  if (network === "mainnet" && process.env.METEORA_ALLOW_MAINNET !== "1") {
    throw new Error(
      "mainnet send blocked: set METEORA_ALLOW_MAINNET=1 in the environment " +
        "and pass --network mainnet explicitly"
    );
  }
}

export function explorerTxUrl(signature: string, network: Network): string {
  const base = `https://explorer.solana.com/tx/${signature}`;
  return network === "devnet" ? `${base}?cluster=devnet` : base;
}
