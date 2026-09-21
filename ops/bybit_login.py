#!/usr/bin/env python3
"""
bybit_login.py — подключить счёт Bybit к роботу.

У биржи нет «входа по паролю»: доступ от вашего имени даёт API-ключ.
Этот мастер спрашивает ключ, проверяет его на бирже (права, срок,
привязка к IP, баланс), и только после успешной проверки кладёт его
в ops/.env — туда, откуда его берёт торговый процесс.

Пароль от аккаунта Bybit роботу не нужен и вводить его сюда нельзя
ни при каких обстоятельствах. Если что-то просит пароль от биржи —
это не робот.

Где взять ключ:

  TESTNET  https://testnet.bybit.com/app/user/api-management
  MAINNET  https://www.bybit.com/app/user/api-management

  Права: API Key Permissions → Read-Write,
         Unified Trading → Trade (или Contract → Order + Position).
  Withdraw НЕ ставить — робот такой ключ не примет.
  IP: укажите адрес машины, где живёт робот. Без привязки ключ
      работает откуда угодно, в том числе у того, кто его украл.

Запуск:
    python ops/bybit_login.py                 мастер подключения (testnet)
    python ops/bybit_login.py --mainnet       подключить боевой ключ
    python ops/bybit_login.py --status        что подключено сейчас
    python ops/bybit_login.py --check         перепроверить ключ на бирже
    python ops/bybit_login.py --logout        забыть ключ
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cashmash.exchange import credentials as cr   # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TESTNET_URL = "https://testnet.bybit.com/app/user/api-management"
MAINNET_URL = "https://www.bybit.com/app/user/api-management"
MAINNET_PHRASE = "БОЕВОЙ"


def env_path() -> Path:
    return ROOT / "ops" / ".env"


def config_network(config: str) -> bool | None:
    """Какую сеть ждёт торговый конфиг. None — прочитать не удалось.

    Сверка нужна потому, что сеть выбирает КОНФИГ, а не ключ: ключ от
    другой сети даст при старте «неверный ключ», и искать причину
    пользователь будет в ключе, хотя дело в несовпадении.
    """
    path = ROOT / config
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    ex = data.get("exchange") or {}
    value = ex.get("testnet")
    return bool(value) if value is not None else None


def show(check: cr.KeyCheck) -> None:
    """Печать результата проверки. Секрет не печатается никогда."""
    print()
    print(f"  Сеть:      {check.network}")
    print(f"  Ключ:      {check.masked_key}"
          + (f"  ({check.label})" if check.label else ""))
    if check.uid:
        print(f"  UID:       {check.uid}")
    if check.permissions:
        rights = ", ".join(f"{k}: {'/'.join(v)}"
                           for k, v in sorted(check.permissions.items()))
        print(f"  Права:     {rights}")
    print(f"  Торговля:  {'да' if check.can_trade and not check.read_only else 'НЕТ'}")
    print(f"  Вывод:     {'ЕСТЬ — недопустимо' if check.withdraw else 'нет — правильно'}")
    print(f"  IP:        {', '.join(check.ips) if check.ips else 'без привязки'}")
    if check.expires_in_days is not None:
        print(f"  Срок:      ещё {check.expires_in_days} дн.")
    if check.equity is not None:
        print(f"  Баланс:    {check.equity} USDT")
    for p in check.problems:
        print(f"\n  ✗ {p}")
    for w in check.warnings:
        print(f"  ⚠ {w}")
    print()


def ask_secret(prompt: str) -> str:
    """Секрет с экрана не читается.

    Если ввод не с терминала (конвейер, запуск из другой программы) —
    читаем строку как есть: прятать нечего, и падать тоже незачем.
    """
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def cmd_status(config: str) -> int:
    creds = cr.load(env_path())
    if not creds.present:
        print("\n  Счёт не подключён. Подключить:  python ops/bybit_login.py\n")
        return 1
    print(f"\n  Ключ {creds.masked} · {creds.network} · источник: {creds.source}")
    want = config_network(config)
    if want is not None and want != creds.testnet:
        print(f"  ⚠ конфиг {config} ждёт "
              f"{'TESTNET' if want else 'MAINNET'}, а ключ от {creds.network}:")
        print("    робот получит от биржи «неверный ключ». Подключите ключ "
              "нужной сети\n      или смените конфиг.")
    print("\n  Проверить ключ на бирже:  python ops/bybit_login.py --check\n")
    return 0


def cmd_check(config: str) -> int:
    creds = cr.load(env_path())
    if not creds.present:
        print("\n  Проверять нечего: счёт не подключён.\n")
        return 1
    print(f"\n  Спрашиваю биржу про ключ {creds.masked} ({creds.network})…")
    check = cr.verify(creds)
    cr.remember(check, ROOT)
    show(check)
    want = config_network(config)
    if want is not None and want != creds.testnet:
        print(f"  ⚠ конфиг {config} ждёт "
              f"{'TESTNET' if want else 'MAINNET'}, ключ от {creds.network}\n")
    return 0 if check.ok else 2


def cmd_logout() -> int:
    had = cr.forget(env_path())
    cr.cache_path(ROOT).unlink(missing_ok=True)
    if had:
        print("\n  Ключ удалён из ops/.env.")
        print("  На самой бирже он продолжает существовать — если вы "
              "отключаете\n  робота насовсем, удалите ключ и в кабинете Bybit.")
        print("\n  Торговый процесс перейдёт в MANAGE_ONLY: новых входов "
              "не будет,\n  уже открытая позиция останется под присмотром "
              "биржевого стопа.\n")
    else:
        print("\n  Ключа и не было.\n")
    return 0


def cmd_login(testnet: bool, config: str) -> int:
    url = TESTNET_URL if testnet else MAINNET_URL
    net = "TESTNET" if testnet else "MAINNET"

    print()
    print("  ╔════════════════════════════════════════════════════════╗")
    print(f"  ║  Подключение счёта Bybit к роботу · {net:<19}║")
    print("  ╚════════════════════════════════════════════════════════╝")
    print()
    if not testnet:
        print("  ⚠ БОЕВОЙ КОНТУР. Ключ даст роботу распоряжаться настоящими")
        print("    деньгами на вашем счёте. Начинать здесь не нужно:")
        print("    сначала testnet, там те же движения и виртуальные деньги.")
        print()
    print(f"  1. Откройте {url}")
    print("  2. Create New Key → System-generated API Keys")
    print("  3. Права: Read-Write; Unified Trading → Trade")
    print("     (на классическом счёте: Contract → Order + Position)")
    print("  4. Withdraw НЕ отмечать — робот такой ключ не примет")
    print("  5. IP: адрес машины, где работает робот")
    print()
    print("  Секрет Bybit показывает ОДИН раз. Скопируйте его сразу.")
    print("  Пароль от аккаунта сюда вводить нельзя: он роботу не нужен.")
    print()

    key = ask("  API Key:    ")
    if not key:
        print("\n  Отменено.\n")
        return 1
    secret = ask_secret("  API Secret: ")
    if not secret:
        print("\n  Отменено: секрет не введён.\n")
        return 1

    creds = cr.Credentials(key=key, secret=secret, testnet=testnet)
    print(f"\n  Спрашиваю биржу…")
    check = cr.verify(creds)
    cr.remember(check, ROOT)
    show(check)

    if not check.ok:
        print("  Ключ НЕ сохранён — сначала устраните причину выше.\n")
        return 2

    if not testnet:
        print(f"  Это боевой счёт. Чтобы подтвердить, наберите {MAINNET_PHRASE}")
        if ask("  Подтверждение: ") != MAINNET_PHRASE:
            print("\n  Отменено. Ключ не сохранён.\n")
            return 1

    note = cr.save(creds, env_path())
    print(f"  ✓ Ключ сохранён в ops/.env ({note}).")
    print("    Файл в git не попадает — он в .gitignore с первого коммита.")

    want = config_network(config)
    if want is not None and want != testnet:
        print()
        print(f"  ⚠ Конфиг {config} ждёт "
              f"{'TESTNET' if want else 'MAINNET'}, а ключ от {net}.")
        print("    Сеть выбирает конфиг, поэтому робот получит от биржи")
        print("    «неверный ключ». Возьмите конфиг нужной сети "
              "(--config)\n    или подключите ключ той сети, что в конфиге.")
    if not testnet:
        print()
        print("  Боевой конфиг дополнительно требует confirm_mainnet: true —")
        print("  без него робот не стартует. Это сделано намеренно.")

    print()
    print("  Что дальше:")
    print("    • если робот запущен (python start.py или ops/run.py) —")
    print("      он заметит новый ключ сам и перезапустит торговый процесс;")
    print("    • если не запущен — запустите: python start.py")
    print()
    print("  С этого момента робот ходит на биржу от вашего имени: читает")
    print("  баланс и позиции, ставит и снимает заявки, держит стоп на")
    print("  бирже. Входы в рынок включает режим LIVE в конфиге и наличие")
    print("  сигнального слоя — см. README, раздел «Подключение биржи».")
    print()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mainnet", action="store_true",
                    help="боевой контур вместо testnet")
    ap.add_argument("--config", default="config/testnet.yaml",
                    help="конфиг, с которым сверяется сеть ключа")
    ap.add_argument("--status", action="store_true",
                    help="показать, что подключено, и выйти")
    ap.add_argument("--check", action="store_true",
                    help="перепроверить сохранённый ключ на бирже")
    ap.add_argument("--logout", action="store_true",
                    help="забыть ключ (на бирже он остаётся)")
    args = ap.parse_args()

    if args.status:
        sys.exit(cmd_status(args.config))
    if args.check:
        sys.exit(cmd_check(args.config))
    if args.logout:
        sys.exit(cmd_logout())
    sys.exit(cmd_login(testnet=not args.mainnet, config=args.config))


if __name__ == "__main__":
    main()
