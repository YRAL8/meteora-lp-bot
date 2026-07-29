/**
 * Fast devnet pool lookup via getProgramAccounts (WSOL + Circle devnet USDC).
 */
import { Connection, PublicKey } from "@solana/web3.js";
import DLMM from "@meteora-ag/dlmm";

const PROGRAM = new PublicKey("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo");
const WSOL = new PublicKey("So11111111111111111111111111111111111111112");
const DEVNET_USDC = new PublicKey("4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU");
const RPC = process.env.SOLANA_DEVNET_RPC_URL || "https://api.devnet.solana.com";

async function main(): Promise<void> {
  const conn = new Connection(RPC, "confirmed");
  const filters = [
    { memcmp: { offset: 88, bytes: WSOL.toBase58() } },
    { memcmp: { offset: 120, bytes: DEVNET_USDC.toBase58() } },
  ];
  const accs = await conn.getProgramAccounts(PROGRAM, {
    filters,
    dataSlice: { offset: 0, length: 0 },
  });
  const hits: Array<{
    pubkey: string;
    binStep: number;
    activeId: number;
    reserveSol: number;
    reserveUsdc: number;
  }> = [];
  for (const a of accs.slice(0, 20)) {
    try {
      const dlmm = await DLMM.create(conn, a.pubkey);
      await dlmm.refetchStates();
      const x = dlmm.tokenX.publicKey.toBase58();
      const solIsX = x === WSOL.toBase58();
      const rx = Number(dlmm.tokenX.amount) / 10 ** dlmm.tokenX.mint.decimals;
      const ry = Number(dlmm.tokenY.amount) / 10 ** dlmm.tokenY.mint.decimals;
      const reserveSol = solIsX ? rx : ry;
      const reserveUsdc = solIsX ? ry : rx;
      hits.push({
        pubkey: a.pubkey.toBase58(),
        binStep: dlmm.lbPair.binStep,
        activeId: dlmm.lbPair.activeId,
        reserveSol,
        reserveUsdc,
      });
      await new Promise((r) => setTimeout(r, 400));
    } catch {
      /* skip */
    }
  }
  hits.sort(
    (a, b) => b.reserveSol * b.reserveUsdc - a.reserveSol * a.reserveUsdc
  );
  console.log(JSON.stringify({ scanned: accs.length, top: hits.slice(0, 5) }, null, 2));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
