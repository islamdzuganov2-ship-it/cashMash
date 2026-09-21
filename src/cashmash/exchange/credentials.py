"""Ключи Bybit: ввод, проверка, хранение.

Биржа не знает слова «логин»: у неё нет сессии, которую можно открыть.
Доступ от имени пользователя — это API-ключ и секрет, которыми
подписывается каждый запрос. Поэтому «войти в Bybit через робота»
означает ровно одно: передать роботу ключ, убедиться, что ключ рабочий
и безопасный, и положить его туда, откуда его возьмёт торговый процесс.

Четыре решения, которые стоит объяснить.

**Проверка до сохранения.** Ключ не записывается, пока биржа не
подтвердила его подписанным запросом. Иначе диагноз «робот запустился и
молча не торгует» приходится ставить по логам — а причиной оказывается
опечатка в секрете или ключ не от той сети.

**Ключ с правом вывода не принимается вовсе.** Это не перестраховка:
docs/10-Security.md, 10.2 — «Withdraw — НИКОГДА». Робот, которому дали
право выводить средства, превращает любую утечку файла в потерю счёта,
а торговле это право не нужно ни в одном сценарии.

**Секрет живёт только в файле и в памяти процесса.** Он не попадает ни
в лог, ни в heartbeat, ни в ответ панели: наружу уходит только маска
ключа (первые четыре символа) — её хватает, чтобы отличить один ключ от
другого, и не хватает, чтобы им воспользоваться.

**Сеть — часть удостоверения.** Ключ testnet на боевом контуре не
работает и наоборот, а сообщение биржи в обоих случаях одинаковое —
«неверный ключ». Поэтому сеть хранится рядом с ключом и сверяется
с конфигом торгового процесса.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

ENV_KEY = "BYBIT_API_KEY"
ENV_SECRET = "BYBIT_API_SECRET"
ENV_TESTNET = "BYBIT_TESTNET"

_TRUE = {"1", "true", "yes", "on", "да"}
_FALSE = {"0", "false", "no", "off", "нет"}

# Право, которого у торгового ключа быть не должно ни при каких условиях.
WITHDRAW = "Withdraw"

# Группы прав, любая из которых даёт торговлю: имя группы зависит от
# того, унифицированный счёт или классический.
TRADE_GROUPS = ("ContractTrade", "Derivatives", "Options", "Spot")


def mask(key: str) -> str:
    """Ключ в логах, на панели и в алертах — только префикс.

    docs/10-Security.md, 10.8: логи не содержат ключей. Четырёх символов
    достаточно, чтобы ответить на вопрос «тот ли ключ подставился».
    """
    key = key.strip()
    if not key:
        return "—"
    return f"{key[:4]}…" if len(key) > 4 else "…"


def parse_bool(value: str, default: bool = True) -> bool:
    v = value.strip().strip('"').strip("'").lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return default


@dataclass(frozen=True, slots=True)
class Credentials:
    """Пара ключей и сеть, к которой они относятся."""

    key: str = ""
    secret: str = ""
    testnet: bool = True
    source: str = "нет"          # откуда взялись: окружение или файл

    @property
    def present(self) -> bool:
        return bool(self.key.strip() and self.secret.strip())

    @property
    def masked(self) -> str:
        return mask(self.key)

    @property
    def network(self) -> str:
        return "TESTNET" if self.testnet else "MAINNET"

    def __repr__(self) -> str:                      # секрет не печатается
        return (f"Credentials(key={self.masked!r}, network={self.network!r}, "
                f"source={self.source!r})")


# ----------------------------------------------------------------------
# где лежит файл секретов


def default_env_path() -> Path:
    """`ops/.env` проекта. Переопределяется `CASHMASH_ENV_FILE`.

    Переопределение нужно двум сценариям: контейнеру, где секреты
    смонтированы в другое место, и тестам, которым нельзя прикасаться
    к настоящему файлу.
    """
    override = os.environ.get("CASHMASH_ENV_FILE", "").strip()
    if override:
        return Path(override)
    root = Path(__file__).resolve().parents[3]
    if (root / "ops").is_dir():
        return root / "ops" / ".env"
    return Path.cwd() / "ops" / ".env"


def read_env_file(path: Path) -> dict[str, str]:
    """Пары ключ-значение из .env. Отсутствие файла — пустой словарь."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        out[name.strip()] = value.strip().strip('"').strip("'")
    return out


def harden(path: Path) -> str:
    """Закрыть файл от прочих пользователей машины. Возвращает пояснение.

    На POSIX это права 600. На Windows `chmod` умеет только снимать и
    ставить «только для чтения» — реальное ограничение делает `icacls`,
    и если он недоступен, об этом надо сказать вслух, а не молча
    оставить секрет читаемым всем в системе.
    """
    if os.name != "nt":
        try:
            path.chmod(0o600)
            return "права 600"
        except OSError as exc:
            return f"права выставить не удалось: {exc}"

    user = os.environ.get("USERNAME", "").strip()
    if not user:
        return "владелец файла не определён — проверьте доступ вручную"
    try:
        r = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"icacls недоступен ({exc}) — проверьте доступ вручную"
    if r.returncode != 0:
        return "icacls отказал — проверьте доступ к файлу вручную"
    return f"доступ только у {user}"


def write_env_file(path: Path, updates: dict[str, str | None]) -> str:
    """Записать значения в .env, сохранив всё остальное.

    Значение `None` удаляет строку. Запись атомарная: сначала временный
    файл рядом, потом замена. Иначе падение в середине перезаписи
    оставит пользователя и без старого ключа, и без нового — а заодно
    без токена Telegram, который лежит в том же файле.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(encoding="utf-8",
                               errors="replace").splitlines()
    except OSError:
        lines = []

    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        name = raw.split("=", 1)[0].strip() if "=" in raw else ""
        if name and name in updates and not raw.strip().startswith("#"):
            seen.add(name)
            value = updates[name]
            if value is not None:
                out.append(f"{name}={value}")
            continue
        out.append(raw)

    added = [f"{k}={v}" for k, v in updates.items()
             if v is not None and k not in seen]
    if added:
        if out and out[-1].strip():
            out.append("")
        out.append("# --- Ключи биржи: записаны роботом, "
                   "редактировать вручную не нужно ---")
        out.extend(added)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(out).rstrip("\n") + "\n")
        harden(tmp_path)
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    return harden(path)


def load(env_path: Path | None = None) -> Credentials:
    """Ключи: сначала окружение, затем файл.

    Приоритет у окружения — так контейнер или systemd-юнит перекрывают
    файл, не редактируя его. Сеть по умолчанию TESTNET: ошибка в эту
    сторону стоит виртуальных денег, в обратную — настоящих.
    """
    key = os.environ.get(ENV_KEY, "").strip()
    secret = os.environ.get(ENV_SECRET, "").strip()
    if key and secret:
        return Credentials(key, secret,
                           parse_bool(os.environ.get(ENV_TESTNET, "true")),
                           source="окружение")

    return from_file(env_path or default_env_path())


def from_file(path: Path) -> Credentials:
    """Ключи из файла, минуя окружение."""
    data = read_env_file(path)
    key = data.get(ENV_KEY, "").strip()
    secret = data.get(ENV_SECRET, "").strip()
    if key and secret:
        return Credentials(key, secret,
                           parse_bool(data.get(ENV_TESTNET, "true")),
                           source=str(path))
    return Credentials()


def save(creds: Credentials, env_path: Path | None = None) -> str:
    """Записать ключи в .env и вернуть пояснение о правах на файл."""
    path = env_path or default_env_path()
    return write_env_file(path, {
        ENV_KEY: creds.key.strip(),
        ENV_SECRET: creds.secret.strip(),
        ENV_TESTNET: "true" if creds.testnet else "false",
    })


def forget(env_path: Path | None = None) -> bool:
    """Убрать ключи из файла. Возвращает, было ли что убирать.

    Строки удаляются целиком, а не затираются пустым значением: пустое
    значение неотличимо от «ещё не заполняли», а разница важна при
    разборе инцидента.
    """
    path = env_path or default_env_path()
    data = read_env_file(path)
    had = bool(data.get(ENV_KEY) or data.get(ENV_SECRET))
    write_env_file(path, {ENV_KEY: None, ENV_SECRET: None, ENV_TESTNET: None})
    return had


# ----------------------------------------------------------------------
# проверка ключа на бирже


class _Client(Protocol):
    """То немногое, что нужно проверке. Позволяет подставить мок."""

    def sync_clock(self) -> tuple[bool, int]: ...

    def query_api(self) -> Any: ...

    def wallet(self, account_type: str = ...) -> Any: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class KeyCheck:
    """Результат проверки ключа.

    `problems` — то, что делает торговлю невозможной или опасной:
    при непустом списке ключ не сохраняется. `warnings` — то, о чём
    обязан знать пользователь, но что он вправе принять.
    """

    ok: bool = False
    network: str = "TESTNET"
    masked_key: str = "—"
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    can_trade: bool = False
    read_only: bool = True
    withdraw: bool = False
    permissions: dict[str, list[str]] = field(default_factory=dict)
    ips: list[str] = field(default_factory=list)
    expires_in_days: int | None = None
    label: str = ""
    uid: str = ""
    unified: bool = False
    equity: str | None = None
    checked_at_ms: int = 0

    def summary(self) -> str:
        if self.problems:
            return self.problems[0]
        if not self.ok:
            return "ключ не проверен"
        where = "боевой счёт" if self.network == "MAINNET" else "тестовый счёт"
        money = f", баланс {self.equity} USDT" if self.equity is not None else ""
        return f"ключ {self.masked_key} принят биржей · {where}{money}"

    def as_dict(self) -> dict[str, Any]:
        """Для панели и heartbeat. Секрета здесь нет и быть не может."""
        return {
            "ok": self.ok,
            "network": self.network,
            "key": self.masked_key,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "can_trade": self.can_trade,
            "read_only": self.read_only,
            "withdraw": self.withdraw,
            "permissions": {k: list(v) for k, v in self.permissions.items()},
            "ips": list(self.ips),
            "expires_in_days": self.expires_in_days,
            "label": self.label,
            "uid": self.uid,
            "unified": self.unified,
            "equity": self.equity,
            "checked_at_ms": self.checked_at_ms,
            "summary": self.summary(),
        }


def _equity_of(result: dict[str, Any]) -> str | None:
    for acc in result.get("list") or []:
        raw = acc.get("totalEquity")
        if raw in (None, ""):
            continue
        try:
            return str(Decimal(str(raw)).quantize(Decimal("0.01")))
        except (InvalidOperation, ValueError):
            return str(raw)
    return None


def _auth_hint(creds: Credentials, detail: str, ret_code: Any) -> str:
    """Перевод отказа биржи на язык причин.

    Биржа на три разные беды — не тот ключ, не та сеть, не тот IP —
    отвечает почти одинаково. Различить их постфактум по логу нельзя,
    а в момент ввода можно: мы знаем, какую сеть выбрал пользователь.
    """
    if str(ret_code) == "10010":
        return ("ключ привязан к другому IP: Bybit видит запрос не с того "
                "адреса. Добавьте текущий адрес в белый список ключа")
    other = "MAINNET" if creds.testnet else "TESTNET"
    return (f"биржа не приняла ключ ({detail}). Обычные причины: ключ от "
            f"{other} — сейчас выбран {creds.network}; опечатка в секрете; "
            f"ключ уже удалён в кабинете Bybit")


def verify(creds: Credentials, *, account_type: str = "UNIFIED",
           client: _Client | None = None) -> KeyCheck:
    """Спросить у биржи, что это за ключ и на что он годен.

    Порядок неслучаен. Сначала синхронизация часов: запрос с уехавшей
    меткой времени биржа отклонит как неверно подписанный, и мы обвиним
    в этом ключ. Затем `/v5/user/query-api` — права, срок, привязка
    к IP. И только потом баланс: он отвечает на вопрос «есть ли чем
    торговать», но на пригодность ключа не влияет.
    """
    from .errors import ErrorClass
    from .rest import BybitRest

    if not creds.present:
        return KeyCheck(problems=["ключ и секрет не заданы"],
                        network=creds.network)

    cl: _Client = client or BybitRest(api_key=creds.key,
                                      api_secret=creds.secret,
                                      testnet=creds.testnet)
    try:
        cl.sync_clock()
        resp = cl.query_api()

        problems: list[str] = []
        warnings: list[str] = []

        if not resp.ok:
            detail = resp.verdict.detail or "без пояснения"
            if resp.verdict.cls is ErrorClass.AUTH:
                problems.append(_auth_hint(creds, detail,
                                           resp.raw.get("retCode")))
            elif resp.verdict.cls is ErrorClass.CLOCK:
                problems.append("часы машины разошлись с биржей — "
                                "синхронизируйте время и повторите")
            else:
                problems.append(
                    f"биржа недоступна или ответила отказом: {detail}")
            return KeyCheck(network=creds.network, masked_key=creds.masked,
                            problems=problems,
                            checked_at_ms=int(time.time() * 1000))

        r = resp.result
        perms: dict[str, list[str]] = {
            str(k): [str(x) for x in (v or [])]
            for k, v in (r.get("permissions") or {}).items() if v
        }
        read_only = str(r.get("readOnly", "1")) not in ("0", "false", "False")
        withdraw = any(WITHDRAW in v for v in perms.values())
        can_trade = any(perms.get(g) for g in TRADE_GROUPS)
        ips = [str(x) for x in (r.get("ips") or [])]

        if withdraw:
            problems.append(
                "у ключа есть право вывода средств (Withdraw). Робот такой "
                "ключ не принимает: торговле это право не нужно, а утечка "
                "файла с ним означает потерю счёта. Выпустите ключ без "
                "Withdraw")
        if read_only:
            problems.append("ключ только для чтения (Read-Only) — торговать "
                            "им нельзя, нужен ключ с правом Trade")
        if not can_trade:
            problems.append("у ключа нет прав на торговлю деривативами: "
                            "включите Contract / Derivatives Trade")

        if not ips or "*" in ips:
            warnings.append("ключ не привязан к IP — утёкший ключ будет "
                            "работать откуда угодно "
                            "(docs/10-Security.md, 10.2)")
        days = _deadline_days(r.get("deadlineDay"))
        if days is not None and days <= 14:
            warnings.append(f"ключ истекает через {days} дн. — после этого "
                            f"робот перестанет торговать молча")
        if not creds.testnet:
            warnings.append("это БОЕВОЙ контур: сделки идут "
                            "на настоящие деньги")

        equity: str | None = None
        if not problems:
            w = cl.wallet(account_type)
            if w.ok:
                equity = _equity_of(w.result)
                if equity is not None and Decimal(equity) <= 0:
                    warnings.append("на счёте нет средств — "
                                    "торговать не на что")
            else:
                warnings.append("баланс прочитать не удалось: "
                                f"{w.verdict.detail or 'без пояснения'}")

        return KeyCheck(
            ok=not problems,
            network=creds.network,
            masked_key=creds.masked,
            problems=problems,
            warnings=warnings,
            can_trade=can_trade,
            read_only=read_only,
            withdraw=withdraw,
            permissions=perms,
            ips=ips,
            expires_in_days=days,
            label=str(r.get("note") or ""),
            uid=str(r.get("userID") or ""),
            unified=str(r.get("unified", "0")) == "1",
            equity=equity,
            checked_at_ms=int(time.time() * 1000),
        )
    finally:
        if client is None:
            cl.close()


def _deadline_days(raw: Any) -> int | None:
    """Сколько дней ключу осталось. Bybit отдаёт это только для ключей
    без привязки к IP — у привязанных срока нет, и это не ошибка."""
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# память о последней проверке


def cache_path(root: Path | None = None) -> Path:
    """Файл с результатом последней проверки ключа.

    Он существует ради панели. Панель — тонкий клиент: она читает файлы
    и не ходит на биржу, иначе её зависший запрос стал бы отказом
    наблюдения в момент, когда наблюдение нужнее всего. Проверку делает
    тот, кто вводит ключ, и оставляет здесь свой протокол.
    """
    base = root or Path(__file__).resolve().parents[3]
    return base / "data" / "exchange_key.json"


def remember(check: KeyCheck, root: Path | None = None) -> None:
    """Запомнить протокол проверки. Секретов в нём нет — только маска."""
    import json

    path = cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(check.as_dict(), ensure_ascii=False,
                                   indent=1), encoding="utf-8")
    except OSError:
        pass


def recall(root: Path | None = None) -> dict[str, Any] | None:
    import json

    try:
        data = json.loads(cache_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def status(env_path: Path | None = None,
           root: Path | None = None) -> dict[str, Any]:
    """Состояние подключения для панели и отчётов.

    Соединяет два разных факта, которые легко спутать: ключ ЕСТЬ
    (лежит в файле) и ключ ПРОВЕРЕН (биржа его приняла). Ключ могли
    отозвать в кабинете Bybit минуту назад — файл об этом не знает,
    поэтому «проверен» всегда с датой, а не просто галочка.
    """
    creds = load(env_path)
    last = recall(root)
    if last is not None and last.get("key") not in (creds.masked, None):
        last = None                       # протокол от другого ключа

    # Расхождение файла и окружения. Возникает так: робот запущен с
    # ключом в окружении, а ключ в файле потом поменяли. Процесс, уже
    # получивший переменную, о подмене не знает — и отчёт, умалчивающий
    # об этом, показал бы один ключ там, где работает другой.
    in_file = from_file(env_path or default_env_path())
    conflict = (creds.source == "окружение" and in_file.present
                and in_file.key != creds.key)

    return {
        "connected": creds.present,
        "key": creds.masked,
        "network": creds.network,
        "testnet": creds.testnet,
        "source": creds.source if creds.present else "",
        "verified": bool(last and last.get("ok")),
        "conflict": conflict,
        "file_key": in_file.masked if conflict else "",
        "last_check": last,
    }
