/**
 * One-off: find devnet DLMM pools with WSOL + Circle devnet USDC.
 * Run: node ts/dist/find_devnet_pool.js
 */
import { Connection, PublicKey } from "@solana/web3.js";
import DLMM from "@meteora-ag/dlmm";

const WSOL = "So11111111111111111111111111111111111111112";
const DEVNET_USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU";
const RPC = process.env.SOLANA_DEVNET_RPC_URL || "https://api.devnet.solana.com";

async function main(): Promise<void> {
  const conn = new Connection(RPC, "confirmed");
  const pairs = await DLMM.getLbPairs(conn, { cluster: "devnet" });
  const hits: Array<{
    pubkey: string;
    binStep: number;
    activeId: number;
    reserveSol: number;
    reserveUsdc: number;
  }> = [];
  for (const p of pairs) {
    const pk = p.publicKey.toBase58();
    try {
      const dlmm = await DLMM.create(conn, p.publicKey);
      await dlmm.refetchStates();
      const x = dlmm.tokenX.publicKey.toBase58();
      const y = dlmm.tokenY.publicKey.toBase58();
      const hasWsol = x === WSOL || y === WSOL;
      const hasUsdc = x === DEVNET_USDC || y === DEVNET_USDC;
      if (!hasWsol || !hasUsdc) continue;
      const solIsX = x === WSOL;
      const solDec = solIsX
        ? dlmm.tokenX.mint.decimals
        : dlmm.tokenY.mint.decimals;
      const usdcDec = solIsX
        ? dlmm.tokenY.mint.decimals
        : dlmm.tokenX.mint.decimals;
      const rx = Number(dlmm.tokenX.amount) / 10 ** dlmm.tokenX.mint.decimals;
      const ry = Number(dlmm.tokenY.amount) / 10 ** dlmm.tokenY.mint.decimals;
      const reserveSol = solIsX ? rx : ry;
      const reserveUsdc = solIsX ? ry : rx;
      if (reserveSol < 0.01 || reserveUsdc < 1) continue;
      hits.push({
        pubkey: pk,
        binStep: dlmm.lbPair.binStep,
        activeId: dlmm.lbPair.activeId,
        reserveSol,
        reserveUsdc,
      });
    } catch {
      /* skip broken */
    }
  }
  hits.sort(
    (a, b) => b.reserveSol * b.reserveUsdc - a.reserveSol * a.reserveUsdc
  );
  console.log(JSON.stringify({ count: hits.length, top: hits.slice(0, 5) }, null, 2));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
