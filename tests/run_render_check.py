#!/usr/bin/env python3
"""C7: проверить отображение сообщений бота через настоящий Telegram Bot API.

Денежные exec_* подменяются заглушками — реальных транзакций нет.
Читающие пути (/status и т.п.) могут ходить в RPC.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault(
    "WALLET_KEYPAIR_PATH",
    os.path.expanduser("~/.config/solana/meteora-devnet.json"),
)

import bot_config  # noqa: E402
import money_ops  # noqa: E402
import requests  # noqa: E402
import telegram_commands as tg  # noqa: E402
import telegram_keyboard as kb  # noqa: E402
from money_ops import StepNotes  # noqa: E402
from telegram_notify import (  # noqa: E402
    TG_MAX_MESSAGE_LEN,
    escape_html,
    format_exec_replies,
    format_html_error,
)

# --- Safety: stub every on-chain money path before any case runs ---
FAKE_SIG = "C7RenderCheckFakeSig111111111111111111111111111111111111111111"
FAKE_EXPLORER = (
    f"https://explorer.solana.com/tx/{FAKE_SIG}?cluster=devnet"
)
_EXEC_CALLS: list[str] = []


def _fake_exec_payload(**extra: Any) -> dict:
    return {
        "sends": [{"signature": FAKE_SIG, "explorer": FAKE_EXPLORER}],
        "signatures": [FAKE_SIG],
        "params": {"positionPubkey": "C7FakePositionPubkey1111111111111111111111"},
        **extra,
    }


def _stub_exec(name: str):
    def _inner(*_a: Any, **_k: Any) -> dict:
        _EXEC_CALLS.append(name)
        return _fake_exec_payload()

    return _inner


@dataclass
class CaseResult:
    name: str
    ok: bool
    parse_mode: str | None
    message_id: int | None = None
    error: str = ""
    text_preview: str = ""


@dataclass
class RunState:
    results: list[CaseResult] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)


class _FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append((text, dict(kwargs)))


def _ctx(args: list[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.args = list(args or [])
    return ctx


def _update(text: str = "") -> tuple[MagicMock, _FakeMessage]:
    msg = _FakeMessage(text)
    update = MagicMock()
    update.effective_message = msg
    update.effective_chat = MagicMock()
    update.effective_chat.id = int(bot_config.TELEGRAM_CHAT_ID or "0")
    return update, msg


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{bot_config.TELEGRAM_BOT_TOKEN}/{method}"


def send_message(
    text: str, *, parse_mode: str | None, pause_sec: float
) -> tuple[bool, int | None, str]:
    """Send via Bot API; return (ok, message_id, error)."""
    if pause_sec > 0:
        time.sleep(pause_sec)
    payload: dict[str, Any] = {
        "chat_id": bot_config.TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(_api_url("sendMessage"), json=payload, timeout=20)
    except Exception as e:
        return False, None, f"request error: {e}"
    try:
        body = resp.json()
    except Exception:
        return False, None, f"HTTP {resp.status_code}: non-json {resp.text[:300]}"
    if not body.get("ok"):
        desc = body.get("description") or resp.text[:400]
        return False, None, str(desc)
    mid = (body.get("result") or {}).get("message_id")
    return True, int(mid) if mid is not None else None, ""


def delete_message(message_id: int) -> bool:
    try:
        resp = requests.post(
            _api_url("deleteMessage"),
            json={
                "chat_id": bot_config.TELEGRAM_CHAT_ID,
                "message_id": message_id,
            },
            timeout=15,
        )
        body = resp.json()
        return bool(body.get("ok"))
    except Exception:
        return False


def record(
    state: RunState,
    name: str,
    text: str,
    *,
    parse_mode: str | None,
    pause_sec: float,
) -> None:
    preview = text.replace("\n", " ")[:120]
    ok, mid, err = send_message(text, parse_mode=parse_mode, pause_sec=pause_sec)
    state.results.append(
        CaseResult(
            name=name,
            ok=ok,
            parse_mode=parse_mode,
            message_id=mid,
            error=err,
            text_preview=preview,
        )
    )
    if mid is not None:
        state.message_ids.append(mid)
    status = "OK" if ok else f"FAIL: {err}"
    print(f"  [{status}] {name} (len={len(text)}, mode={parse_mode!r})")


async def collect_handler_replies(coro) -> list[tuple[str, dict]]:
    update, msg = _update()
    await coro(update, msg)
    return msg.replies


async def run_cmd(name: str, args: list[str] | None = None) -> list[tuple[str, dict]]:
    update, msg = _update()
    await getattr(tg, f"{name}_command")(update, _ctx(args))
    return msg.replies


async def run_handler(handler, text: str = "") -> list[tuple[str, dict]]:
    update, msg = _update(text)
    await handler(update, _ctx())
    return msg.replies


def _fake_cycles(n: int) -> list[dict]:
    out = []
    for i in range(n):
        out.append(
            {
                "close_time_utc": f"2026-07-{(i % 28) + 1:02d}T12:34:56Z",
                "range_width_pct": float((i % 17) + 1) * 0.15,
                "duration_hours": 2.0 + (i % 7) * 0.25,
                "fees_usd": 0.02 * (i % 11),
                "divergence_usd": -0.03 * (i % 5),
                "pnl_usd": 0.01 * (i % 8) - 0.02,
                "efficiency": (i % 10) / 10.0,
                "incomplete": i % 13 == 0,
            }
        )
    return out


def build_stepnotes_open_message() -> str:
    notes = StepNotes()
    notes.add(
        "⚠️ Бюджет ограничен потолком MAX_POSITION_USD=$0.4, "
        "открываю на $0.40 вместо $5.00"
    )
    notes.add("своп SOL → USDC не прошёл: в пуле не хватило встречной ликвидности")
    notes.add("пересчитал пропорцию под баланс кошелька, без кривой ноги")
    notes.add("урезал депозит под баланс кошелька (×0.812)")
    head = "🆕 Открываю позицию на ~$0.40 (просили $5.00)"
    body = "0.001000 SOL + $0.2000 USDC"
    return f"{head}\n{body}\n{notes.render()}"


def build_cancelled_add_message() -> str:
    notes = StepNotes()
    notes.add(
        f"своп USDC → SOL не прошёл: {escape_html('pool error <binArray> & fail')}"
    )
    detail = notes.render()
    return (
        "❌ Доливка отменена — не доливаю кривой ногой.\n"
        f"{detail}\n"
        "Выровняй баланс кошелька и повтори."
    )


def _synthetic_status_with_pos() -> str:
    from telegram_notify import format_position_table, format_range_bar, short_addr

    pos = {
        "sol": 0.05,
        "usdc": 7.5,
        "lowerBinId": 100,
        "upperBinId": 168,
        "fees": {"sol": 0.0001, "usdc": 0.02},
        "pubkey": "SynPos1111111111111111111111111111111111111",
    }
    price = 150.0
    active = 134
    lines = [
        "📊 <b>Статус [DEMO]</b>",
        format_position_table(pos, price),
        "📈 Цена SOL: $150.00",
        format_range_bar(100, 168, active, price, 1),
        "   Статус: ✅ в диапазоне",
        "Кошелёк: 0.1200 SOL · 10.00 USDC",
        "<i>Новые позиции: ±0.34% (34 ячеек) · потолок $50</i>",
        f"<i>devnet · кошелёк {short_addr('Owner111111111111111111111111111111111111')} · пул "
        f"{short_addr('Pool11111111111111111111111111111111111111')}</i>",
    ]
    return "\n".join(lines)


def _synthetic_status_empty() -> str:
    from telegram_notify import short_addr

    return (
        "📊 <b>Статус [DEMO]</b>\n"
        "⏳ Открытых позиций нет.\n"
        "📈 Цена SOL: $150.00\n"
        "Кошелёк: 0.1200 SOL · 10.00 USDC\n"
        "<i>Новые позиции: ±0.34% (34 ячеек)</i>\n"
        f"<i>devnet · кошелёк {short_addr('Owner111111111111111111111111111111111111')} · пул "
        f"{short_addr('Pool11111111111111111111111111111111111111')}</i>"
    )


def _synthetic_prompts_and_confirms() -> list[tuple[str, str, str | None]]:
    """Шаблоны HTML-экранов на случай, если живой RPC недоступен."""
    return [
        (
            "synth_open_prompt",
            "🆕 <b>Открыть позицию</b>\n"
            "На кошельке: 0.1200 SOL + 10.00 USDC (≈$28)\n"
            "Диапазон сейчас: ±0.34% (34 ячеек)\n\n"
            "Отправь сумму в USDC-эквиваленте (нажми пример):\n"
            "/open 1     /open 5     /open 10",
            "HTML",
        ),
        (
            "synth_add_prompt",
            "➕ <b>Долить</b> в позицию (~$12.50)\n"
            "На кошельке ≈$28; безопасно до ~$10.00.\n"
            "Границы позиции не меняются.\n\n"
            "Отправь сумму или нажми пример:\n"
            "/addliquidity 1     /addliquidity 5     /addliquidity max",
            "HTML",
        ),
        (
            "synth_range_prompt",
            "📐 <b>Диапазон</b>\n"
            "Сейчас: ±0.34% (half=34 ячеек)\n"
            "Максимум на этом пуле (binStep=1): ±0.6990% "
            "(half ≤ 69, ≤70 ячеек в позиции)\n"
            "Минимум ввода: 0.01%\n\n"
            "Отправь процент или нажми пример:\n"
            "/setrange 0.01     /setrange 0.35     /setrange 0.699",
            "HTML",
        ),
        (
            "synth_rebalance_confirm",
            "🔄 <b>Ребаланс</b>\n"
            "Сейчас: $12.50 · bins [100,168]\n"
            "Будет: закрыть → при необходимости свопнуть → открыть вокруг "
            "текущей цены (±0.34% / half=34).\n"
            "Ориентир стоимости перестановки: ~$0.0025 "
            "(≈0.02% от позиции).\n\n"
            "Подтвердить: /rebalance confirm",
            "HTML",
        ),
        (
            "synth_withdraw_confirm",
            "⚠️ <b>Закрыть позицию:</b> $12.50\n"
            "bins [100,168]\n\n"
            "Деньги останутся в кошельке бота — никуда отдельно не переводятся.\n"
            "Подтвердить: /withdraw confirm",
            "HTML",
        ),
        ("synth_status_with_pos", _synthetic_status_with_pos(), "HTML"),
        ("synth_status_empty", _synthetic_status_empty(), "HTML"),
    ]


async def gather_cases() -> list[tuple[str, str, str | None]]:
    """Return list of (name, text, parse_mode)."""
    cases: list[tuple[str, str, str | None]] = []

    # --- Read paths (real RPC where needed) ---
    for label, replies in (
        ("status", await run_cmd("status")),
        ("pnl_live", await run_cmd("pnl")),
        ("help", await run_handler(tg.help_button)),
        ("open_prompt", await run_handler(tg.open_prompt)),
        ("add_prompt", await run_handler(tg.addliquidity_prompt)),
        ("range_prompt", await run_handler(tg.setrange_prompt)),
        ("rebalance_confirm", await run_cmd("rebalance")),
        ("withdraw_confirm", await run_cmd("withdraw")),
    ):
        if not replies:
            cases.append((f"{label}_empty", "⚠️ пустой ответ обработчика", None))
            continue
        for i, (text, kwargs) in enumerate(replies):
            mode = kwargs.get("parse_mode")
            suffix = "" if len(replies) == 1 else f"_{i + 1}"
            cases.append((f"{label}{suffix}", text, mode))

    # /status without position (force empty list + mocked RPC data)
    fake_bal = {"sol": {"ui": 0.12}, "usdc": {"ui": 10.0}}
    fake_pool = {
        "activeId": 134,
        "binStep": 1,
        "usdcPerSol": 150.0,
        "maxBinsPerPosition": 70,
    }
    with (
        patch.object(money_ops, "list_open_positions", return_value=[]),
        patch("telegram_commands.meteora_ops.balances", return_value=fake_bal),
        patch("telegram_commands.meteora_ops.pool_info", return_value=fake_pool),
        patch.object(money_ops, "owner", return_value="Owner111111111111111111111111111111111111"),
    ):
        for i, (text, kwargs) in enumerate(await run_cmd("status")):
            cases.append(
                (f"status_no_position_{i + 1}", text, kwargs.get("parse_mode"))
            )

    # /status with position (mocked RPC — always covers HTML template)
    fake_pos = {
        "sol": 0.05,
        "usdc": 7.5,
        "lowerBinId": 100,
        "upperBinId": 168,
        "fees": {"sol": 0.0001, "usdc": 0.02},
        "pubkey": "SynPos1111111111111111111111111111111111111",
    }
    with (
        patch.object(money_ops, "list_open_positions", return_value=[fake_pos]),
        patch("telegram_commands.meteora_ops.balances", return_value=fake_bal),
        patch("telegram_commands.meteora_ops.pool_info", return_value=fake_pool),
        patch.object(money_ops, "owner", return_value="Owner111111111111111111111111111111111111"),
        patch("telegram_commands.is_reopen_pending", return_value=False),
    ):
        for i, (text, kwargs) in enumerate(await run_cmd("status")):
            cases.append(
                (f"status_with_position_{i + 1}", text, kwargs.get("parse_mode"))
            )

    # Synthetic confirm/prompt screens (same markup as handlers; RPC-independent)
    cases.extend(_synthetic_prompts_and_confirms())

    # /pnl empty journal
    empty_pnl = tg.render_pnl_message(cycles=[], recent_limit=10)
    cases.append(("pnl_empty", empty_pnl, "HTML"))

    # /pnl with 55 cycles (length stress)
    fat_pnl = tg.render_pnl_message(cycles=_fake_cycles(55), recent_limit=10)
    cases.append(("pnl_55_cycles", fat_pnl, "HTML"))
    if len(fat_pnl) > TG_MAX_MESSAGE_LEN:
        raise RuntimeError(
            f"pnl_55_cycles still over limit: {len(fat_pnl)} > {TG_MAX_MESSAGE_LEN}"
        )

    # --- Stubbed money outcomes ---
    cases.append(
        ("exec_success_explorer", format_exec_replies(_fake_exec_payload()), "HTML")
    )
    cases.append(("stepnotes_open", build_stepnotes_open_message(), "HTML"))
    cases.append(("add_cancelled", build_cancelled_add_message(), "HTML"))

    daily = (
        "🛑 <b>Лимит авто-ребалансов на сутки исчерпан</b>\n"
        "Сегодня уже 20 "
        "(MAX_REBALANCES_PER_DAY=20).\n"
        "Автоматика приостановлена до конца суток UTC.\n"
        "Варианты: расширить диапазон (/setrange), сменить пул, "
        "или осознанно поднять MAX_REBALANCES_PER_DAY.\n"
        "Ручной /rebalance по-прежнему доступен."
    )
    cases.append(("auto_rebalance_daily_limit", daily, "HTML"))

    uneconomic = (
        "⚠️ <b>Диагностика окупаемости</b> (не блокирует ребаланс)\n"
        "Ширина ±0.34% на этом пуле не окупает ребалансы: "
        "медианная жизнь последних 5 циклов "
        "<b>1.20 ч</b> против окупаемости <b>3.0 ч</b>.\n"
        "Стоит расширить диапазон (/setrange) или сменить пул на больший binStep.\n"
        "Позиция сейчас ≈ $12.34."
    )
    cases.append(("auto_rebalance_uneconomic", uneconomic, "HTML"))

    # Hostile external text in HTML error path
    hostile = 'boom <b>tag</b> & "q" > 🔥 <script>x</script>'
    cases.append(
        (
            "hostile_error_html",
            format_html_error("❌ Авто-ребаланс ошибка: ", hostile),
            "HTML",
        )
    )
    # Same hostile string as money_ops would emit after escape
    cases.append(
        (
            "hostile_swap_note",
            f"своп SOL → USDC не прошёл: {escape_html(hostile)}",
            "HTML",
        )
    )

    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="C7 Telegram render check")
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Удалить отправленные тестовые сообщения после проверки",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.6,
        help="Пауза между sendMessage (сек)",
    )
    args = parser.parse_args()

    if bot_config.is_placeholder(bot_config.TELEGRAM_BOT_TOKEN):
        print("FAIL: TELEGRAM_BOT_TOKEN не задан")
        return 2
    if bot_config.is_placeholder(bot_config.TELEGRAM_CHAT_ID):
        print("FAIL: TELEGRAM_CHAT_ID не задан")
        return 2

    # Patch exec before any money path could fire (handlers for confirm screens
    # should not call exec; this is belt-and-suspenders).
    patches = [
        patch.object(money_ops.meteora_exec, "exec_open", side_effect=_stub_exec("open")),
        patch.object(money_ops.meteora_exec, "exec_add", side_effect=_stub_exec("add")),
        patch.object(money_ops.meteora_exec, "exec_swap", side_effect=_stub_exec("swap")),
        patch.object(
            money_ops.meteora_exec, "exec_close", side_effect=_stub_exec("close")
        ),
        patch.object(
            money_ops.meteora_exec,
            "exec_close_empty",
            side_effect=_stub_exec("close_empty"),
        ),
        patch.object(
            money_ops.meteora_exec, "exec_withdraw", side_effect=_stub_exec("withdraw")
        ),
    ]
    for p in patches:
        p.start()

    state = RunState()
    try:
        print("Собираю тексты сообщений…")
        cases = asyncio.run(gather_cases())
        header = (
            f"🧪 проверка отображения, {len(cases)} сообщений "
            f"(C7 render check; денежные exec заглушены)"
        )
        print(f"Отправляю заголовок + {len(cases)} сообщений в чат владельца…")
        record(state, "header", header, parse_mode=None, pause_sec=0)
        for name, text, mode in cases:
            record(state, name, text, parse_mode=mode, pause_sec=args.pause)
    finally:
        for p in patches:
            p.stop()

    passed = sum(1 for r in state.results if r.ok)
    failed = [r for r in state.results if not r.ok]
    print("\n=== Сводка ===")
    print(f"проверено: {len(state.results)}  OK: {passed}  FAIL: {len(failed)}")
    print(f"вызовы stub exec_*: {_EXEC_CALLS!r} (ожидается [])")
    if failed:
        print("провалы:")
        for r in failed:
            print(f"  - {r.name}: {r.error}")
            print(f"    preview: {r.text_preview}")

    if args.cleanup and state.message_ids:
        print(f"\n--cleanup: удаляю {len(state.message_ids)} сообщений…")
        deleted = 0
        for mid in state.message_ids:
            if delete_message(mid):
                deleted += 1
            time.sleep(0.3)
        print(f"удалено: {deleted}/{len(state.message_ids)}")

    # Write machine-readable table for REPORT_C7
    out_path = ROOT / "logs" / "c7_render_check.tsv"
    out_path.parent.mkdir(exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        f.write("name\tok\tparse_mode\terror\tpreview\n")
        for r in state.results:
            err = r.error.replace("\t", " ").replace("\n", " ")
            prev = r.text_preview.replace("\t", " ")
            f.write(f"{r.name}\t{int(r.ok)}\t{r.parse_mode}\t{err}\t{prev}\n")
    print(f"таблица: {out_path}")

    if _EXEC_CALLS:
        print("ПРОВАЛ ЗАДАНИЯ: stub exec был вызван — ветка дошла до денег")
        return 3
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
