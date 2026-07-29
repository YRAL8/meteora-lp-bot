# TypeScript layer

Работа с Meteora DLMM через официальный SDK `@meteora-ag/dlmm`. Слой намеренно
разделён на два файла с разными правами:

| файл | что может |
|---|---|
| `src/cli.ts` | **только** сборка + `simulateTransaction`. Не подписывает и не отправляет — никогда. Стерильность проверяется грепом по запрещённым вызовам. |
| `src/exec.ts` | подписывает и отправляет: команды `exec-*`, журнал отправок, двойной замок на mainnet. Отправка только при явном `--send`. |

Ниже описан в основном `cli.ts`; про отправку и её защиты — в корневом
[README](../README.md).

Транзакции SDK (legacy) компилируются в **v0 `VersionedTransaction`** до
симуляции и сериализации (`txs[].kind = "v0"`). На B3 подписывать/отправлять
нужно именно этот объект.

## Сборка и запуск

```bash
cd ts
npm install --ignore-scripts   # если node_modules ещё нет
npx tsc -p tsconfig.json       # → dist/cli.js

node dist/cli.js pool-info
node dist/cli.js balances --owner <PUBKEY>
node dist/cli.js suggest-amounts --owner <PUBKEY> --budget-sol 0.5 --budget-usdc 40
node dist/cli.js build-open --owner <PUBKEY> --sol 0.05 --usdc 3.5 --slippage-bps 100
node dist/cli.js build-swap --owner <PUBKEY> --side sol-to-usdc --amount 0.05
```

Человеческие логи — в stderr. В stdout — **ровно один JSON-объект**.
Если симуляция упала или сработал отказ `multi-tx`, верхний `ok: false`
(при этом `txs` / `simulation` / `params` сохраняются).

Общие флаги: `--rpc <url>` (или `SOLANA_RPC_URL`), `--pool <pubkey>`
(по умолчанию SOL-USDC `5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6`).

Проскальзывание **только** в базисных пунктах: `--slippage-bps`
(по умолчанию `DEFAULT_SLIPPAGE_BPS=100` = 1%). Флаг `--slippage` удалён.
`--priority-fee <микролампорт/CU>` (по умолчанию 0).

Из Python: `meteora_ops.py` (тонкий мост к `node ts/dist/cli.js`).

## Операция → метод SDK

| CLI-команда | Метод `@meteora-ag/dlmm` |
|---|---|
| `pool-info` | `DLMM.create`, `getActiveBin`, `fromPricePerLamport` → `usdcPerSol` |
| `balances` | RPC балансы + ATA; `solAvailableForOpen` = SOL − feeReserve − рента позиции |
| `suggest-amounts` | `autoFillYByStrategy` / `autoFillXByStrategy` → `targetSolFraction`, `needSol`/`needUsdc`, `swapSuggestion` |
| `list-positions` | `getPositionsByUserAndLbPair` |
| `build-open` | `--position-pubkey` или эфемерный pubkey + `initializePositionAndAddLiquidityByStrategy` |
| `build-add` | `addLiquidityByStrategy` / `Chunkable`; без `--allow-multi-tx` отказывает при >1 TX |
| `build-claim-fees` | `claimSwapFee` |
| `build-withdraw` | `removeLiquidity` (`shouldClaimAndClose=false`, `--bps`) |
| `build-close` | `removeLiquidity` (`bps=10000`, `shouldClaimAndClose=true`) |
| `build-close-empty` / `exec-close-empty` | `closePositionIfEmpty` (zero-liq rent reclaim; not in sterile `cli.ts`) |
| `build-swap` | `getBinArrayForSwap` → `swapQuote` → `swap` |

`pool-info` also returns `maxBinsPerPosition` (= SDK `DEFAULT_BIN_PER_POSITION`,
обычно 70). Python считает `max_half_width = (maxBinsPerPosition - 1) // 2`
и потолок `%` для `/setrange`. Хардкодить 34 в Python нельзя.

### `/setrange`: расхождение с Orca

У Orca валидация `1.0 <= pct <= 50`. У нас нижняя граница **0.05%**, верхняя
50, плюс жёсткий потолок `max_pct` от `binStep` и `DEFAULT_BIN_PER_POSITION`
(без тихого обрезания). Причина: при `binStep=1` максимум одной позиции ≈
±0.34%, то есть любой ввод «как у Orca» (≥1%) был бы мёртв; узкие диапазоны в
DLMM — штатный режим концентрации ликвидности.

## Протокол открытия позиции для B3

Секрет ключа позиции **никогда** не пишется в stdout, stderr, JSON или файлы.

1. В **одном процессе** B3: `Keypair.generate()` (или загрузка секрета из
   защищённого хранилища процесса) → взять `publicKey`.
2. Вызвать `build-open --position-pubkey <pk> ...` (dry-run/simulate) **или**
   собрать TX теми же SDK-вызовами внутри того же процесса.
3. Подписать (owner + position keypair) и отправить **сразу**, без паузы.
4. `base64` из холостого прогона с `positionKeypairEphemeral: true` **нельзя**
   переиспользовать для реальной отправки: секрета уже нет.

Если `--position-pubkey` не задан, CLI генерирует эфемерный pubkey только для
проверки сборки и ставит `positionKeypairEphemeral: true` с явной пометкой,
что подписать эту сборку позже невозможно.

## Состояние «закрыл, но не открыл» (фаза C — только документ)

После успешного close капитал лежит на кошельке. Любой сбой на swap/open
оставляет владельца без позиции. Перед автоматическим ребалансом фаза C
обязана персистентно сохранять флаг вроде
`rebalance_reopen_pending=true` (ориентир —
`set_rebalance_reopen_pending` в боевом `orca_bot/orca.py`) и снимать его
только после подтверждённого open. Иначе после timeout RPC непонятно, на
каком шаге остановились.

## Устаревание сборки (фаза C / B3 — только документ)

Между сборкой и отправкой меняются `activeId` и `recentBlockhash`. На B3
транзакция собирается и отправляется **без пауз** в одном процессе. Нельзя
«собрать сейчас, подписать через минуту» по сохранённому base64.

## Известное расхождение комиссий B1 vs SDK

Ридер `meteora_position.py` (фаза B1) суммирует `fee_infos[*].fee_*_pending` и
на позиции `14fT6U72…` показывает нули, тогда как SDK/`list-positions` видит
`feeX=79656`, `feeY=6012`. **Прав SDK**: он досчитывает накопленное с
последнего касания позиции. Файл ридера не меняем в B2.2.

## Чего НЕ умеет `cli.ts`

Именно `cli.ts` — `exec.ts` подписывает и отправляет, см. таблицу в начале файла.

- Не читает файлы ключей / seed / переменные окружения с секретами.
- Не подписывает и не отправляет транзакции в сеть (только симуляция).
- Не принимает решений о ребалансе и не шлёт Telegram — только JSON для Python.
