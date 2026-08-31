# meteora-lp-bot

Liquidity-providing bot for [Meteora DLMM](https://docs.meteora.ag/) pools on Solana,
controlled from Telegram.

**Status: one full mainnet cycle done.** On 29.08.2026 a position was opened, lived in
range and was closed for real money; everything below has also been rehearsed on devnet
with confirmed transactions. A month of cycles is what a verdict needs, and that has not
been run yet.

## How it is put together

The official Meteora SDK exists only in TypeScript, so the project is split in two and
each half does what it is good at:

| | |
|---|---|
| **`ts/src/cli.ts`** | builds transactions and simulates them — never signs, never sends |
| **`ts/src/exec.ts`** | signs and sends, with a journal and a mainnet guard |
| **`*.py`** | decisions, Telegram, bookkeeping — the parts that need no SDK |
| **`meteora_position.py`** | reads positions straight off the chain, decoding accounts by hand without the SDK at all |
| **`pool_snapshots.py`**, **`bin_snapshots.py`** | hourly read-only records of what the pool pays and where its money sits; off the money path, failures stay inside |

Keeping assembly and sending in separate files is deliberate: the file that can move
money is small and easy to audit, and `cli.ts` can be checked mechanically for the
absence of any signing or sending call.

## Safety properties

These are enforced in code, not by convention:

- **Nothing is sent without `--send`.** The default for every `exec-*` command is a dry
  run that stops after simulation.
- **Mainnet needs two independent switches at once** — `--network mainnet` *and*
  `METEORA_ALLOW_MAINNET=1` in the environment. Either one alone is refused.
- **A failed simulation is never sent.** If simulation returns an error, the top-level
  result flips to `ok: false` and the payload keeps the reason instead of burying it.
- **Nonsense amounts are rejected at parse time** — negative, zero, or non-numeric.
- **Every send is journalled before it leaves.** `state/exec_journal.jsonl` records the
  signature first, then the outcome. Three outcomes stay distinct: confirmed, definitely
  failed, and *unknown*. An unknown entry is never retried automatically — a repeated
  open would create a second position — and blocks further operations until resolved.
- **An interrupted rebalance is reported, not hidden.** `state/reopen_pending.json` is
  written before closing and cleared only after a confirmed reopen, so a crash in
  between is announced on the next start.
- **The position keypair never touches disk.** Opening needs a second signature; that
  key is created, used and discarded inside one process, and is never written to stdout,
  logs, or any file.
- **Secrets stay out of the repository.** The wallet key is read from a path given by
  `WALLET_KEYPAIR_PATH`; `.env` and `state/` are ignored by git.

## Telegram commands

Ten commands, deliberately mirroring the sibling `orca-lp-bot` one for one, so both bots
can be operated without switching mental models:

| command | what it does |
|---|---|
| `/status` | balances, open position, price, range, whether price is inside it |
| `/open <usdc>` | opens a position for that budget, working out the SOL/USDC split itself |
| `/addliquidity <usdc>` \| `max` | tops up the existing position within its current range |
| `/withdraw` → `/withdraw confirm` | closes the position in full; works even while frozen, since that is when an emergency exit is needed |
| `/rebalance` | close, rebalance the wallet, reopen around the current price |
| `/setrange <percent>` | range width for future opens |
| `/pnl` | per-cycle journal: fees earned and divergence |
| `/pauza` / `/stop` / `/boevoy` | pause monitoring / freeze everything / resume |

Only the configured chat owner is accepted; one lock serialises every command that moves
money.

## A protocol limit worth knowing

One ordinary position holds at most `DEFAULT_BIN_PER_POSITION` bins (70 in the current
SDK), which caps how wide a range can be — and the cap depends on the pool's bin step:

| bin step | widest single position |
|---|---|
| 1 | ±0.34% |
| 4 | ±1.37% |
| 10 | ±3.46% |

`/setrange` therefore refuses a percentage the pool cannot hold and reports the actual
limit, rather than quietly clamping it — a silent clamp would leave the operator
believing in a range that is not there. Going wider needs either a pool with a coarser
bin step or several position accounts (the SDK supports up to 1400 bins that way), which
is not implemented here.

## What is not done yet

- **One position, so one range.** At bin step 4 that caps a range at ±1.37% (70 bins).
  Going wider needs several position accounts, which is not implemented.
- **No history to judge by.** One mainnet cycle has been run end to end (29.08.2026);
  `/pnl` works but a verdict needs a month of cycles, not one.

## Running it

```bash
cp env_template .env          # then fill in Telegram token, chat id, wallet path
docker compose up -d --build
```

In `.env` set `WALLET_HOST_PATH` to the absolute path of the Solana keypair JSON on
the host (compose mounts it read-only into the container). Named volume
`meteora-lp-bot-state` is declared in `docker-compose.yml` and always bound to
`/app/state` — persistence does not depend on remembering a `-v` flag.

Without Docker: `pip install -r requirements.txt`, build the TypeScript layer with
`cd ts && npm install && npx tsc -p tsconfig.json`, then `python main.py`.

`DRY_RUN=true` in `.env` pins the bot to devnet regardless of anything else.

## Tests

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

All offline — no network, no keys. They cover journal state transitions, the mainnet
guard, amount validation, percent-to-bin conversion, and the branch that closes an empty
position instead of removing liquidity from it.

More detail on the TypeScript layer, including which SDK call backs each operation, is in
[`ts/README.md`](ts/README.md).
