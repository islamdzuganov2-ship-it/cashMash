"""Тесты подключения счёта: хранение ключа и приговор по нему.

Сеть не используется: биржа подменяется клиентом-двойником. Проверяются
ровно те ветки, цена ошибки в которых — чужие деньги: ключ с правом
вывода, ключ только для чтения, ключ не от той сети, а также то, что
секрет не попадает ни в одно место, откуда его можно прочитать.
"""

from __future__ import annotations

from pathlib import Path

from cashmash.exchange import credentials as cr
from cashmash.exchange.errors import classify
from cashmash.exchange.rest import Response


def resp(result: dict, *, ret_code: int = 0) -> Response:
    return Response(ok=ret_code == 0, verdict=classify(ret_code),
                    result=result, http_status=200, rtt_ms=1.0,
                    raw={"retCode": ret_code, "result": result})


class FakeBybit:
    """Двойник биржи: отдаёт заранее заданный паспорт ключа и баланс."""

    def __init__(self, key_info: dict, *, wallet_equity: str = "100",
                 ret_code: int = 0) -> None:
        self.key_info = key_info
        self.wallet_equity = wallet_equity
        self.ret_code = ret_code
        self.closed = False

    def sync_clock(self) -> tuple[bool, int]:
        return True, 0

    def query_api(self) -> Response:
        return resp(self.key_info, ret_code=self.ret_code)

    def wallet(self, account_type: str = "UNIFIED") -> Response:
        return resp({"list": [{"totalEquity": self.wallet_equity}]})

    def close(self) -> None:
        self.closed = True


def passport(**over: object) -> dict:
    base: dict = {
        "apiKey": "ABCD1234EFGH",
        "note": "cashmash",
        "readOnly": 0,
        "permissions": {"ContractTrade": ["Order", "Position"],
                        "Wallet": ["AccountTransfer"]},
        "ips": ["203.0.113.7"],
        "userID": "42",
        "unified": "1",
        "deadlineDay": 90,
    }
    base.update(over)
    return base


# ----------------------------------------------------------------------


class TestMask:
    def test_only_prefix_leaks(self):
        """docs/10-Security.md, 10.8: в логах — префикс, не ключ."""
        m = cr.mask("ABCD1234EFGH5678")
        assert m == "ABCD…"
        assert "1234EFGH5678" not in m

    def test_empty(self):
        assert cr.mask("") == "—"

    def test_secret_never_in_repr(self):
        c = cr.Credentials(key="ABCD1234", secret="s3cr3t-very-long", testnet=True)
        assert "s3cr3t" not in repr(c)
        assert "ABCD…" in repr(c)


class TestEnvFile:
    def test_writes_and_keeps_neighbours(self, tmp_path: Path):
        """Ключ кладётся рядом с токеном Telegram и не затирает его.

        Это не гигиена, а защита от потери: оба секрета живут в одном
        файле, и запись одного не должна стоить другого.
        """
        env = tmp_path / ".env"
        env.write_text("CASHMASH_TG_TOKEN=123:AAH\n"
                       "CASHMASH_TG_CHAT_ID=777\n", encoding="utf-8")
        cr.save(cr.Credentials("ABCD1234", "secret", testnet=True), env)

        data = cr.read_env_file(env)
        assert data["CASHMASH_TG_TOKEN"] == "123:AAH"
        assert data["CASHMASH_TG_CHAT_ID"] == "777"
        assert data[cr.ENV_KEY] == "ABCD1234"
        assert data[cr.ENV_TESTNET] == "true"

    def test_rewrites_in_place(self, tmp_path: Path):
        env = tmp_path / ".env"
        cr.save(cr.Credentials("AAAA1111", "s1", testnet=True), env)
        cr.save(cr.Credentials("BBBB2222", "s2", testnet=False), env)
        text = env.read_text(encoding="utf-8")
        assert text.count(cr.ENV_KEY) == 1
        assert "AAAA1111" not in text
        assert cr.read_env_file(env)[cr.ENV_TESTNET] == "false"

    def test_forget_removes_lines_entirely(self, tmp_path: Path):
        """Пустое значение неотличимо от «не заполняли» — удаляем строку."""
        env = tmp_path / ".env"
        env.write_text("CASHMASH_TG_TOKEN=keep\n", encoding="utf-8")
        cr.save(cr.Credentials("ABCD1234", "secret"), env)

        assert cr.forget(env) is True
        text = env.read_text(encoding="utf-8")
        assert cr.ENV_KEY not in text and "secret" not in text
        assert "CASHMASH_TG_TOKEN=keep" in text
        assert cr.forget(env) is False

    def test_load_prefers_environment(self, tmp_path: Path, monkeypatch):
        env = tmp_path / ".env"
        cr.save(cr.Credentials("FILE1111", "file-secret", testnet=True), env)
        monkeypatch.setenv(cr.ENV_KEY, "ENV22222")
        monkeypatch.setenv(cr.ENV_SECRET, "env-secret")
        monkeypatch.setenv(cr.ENV_TESTNET, "false")

        c = cr.load(env)
        assert c.key == "ENV22222" and c.testnet is False
        assert c.source == "окружение"

    def test_load_falls_back_to_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv(cr.ENV_KEY, raising=False)
        monkeypatch.delenv(cr.ENV_SECRET, raising=False)
        env = tmp_path / ".env"
        cr.save(cr.Credentials("FILE1111", "file-secret", testnet=True), env)

        c = cr.load(env)
        assert c.key == "FILE1111" and c.present and c.network == "TESTNET"

    def test_half_filled_is_not_connected(self, tmp_path: Path, monkeypatch):
        """Ключ без секрета — это не подключение, а мусор в файле."""
        monkeypatch.delenv(cr.ENV_KEY, raising=False)
        monkeypatch.delenv(cr.ENV_SECRET, raising=False)
        env = tmp_path / ".env"
        env.write_text(f"{cr.ENV_KEY}=ABCD1234\n", encoding="utf-8")
        assert cr.load(env).present is False


class TestVerify:
    def test_good_key_is_accepted(self):
        c = cr.Credentials("ABCD1234", "secret", testnet=True)
        check = cr.verify(c, client=FakeBybit(passport()))
        assert check.ok and check.can_trade and not check.read_only
        assert check.equity == "100.00"
        assert check.problems == []

    def test_withdraw_permission_is_refused(self):
        """docs/10-Security.md, 10.2: Withdraw — никогда.

        Торговле это право не нужно, а утечка файла с таким ключом
        означает потерю счёта, а не убыток по сделке.
        """
        p = passport(permissions={"ContractTrade": ["Order"],
                                  "Wallet": ["AccountTransfer", "Withdraw"]})
        check = cr.verify(cr.Credentials("ABCD1234", "s"), client=FakeBybit(p))
        assert not check.ok and check.withdraw
        assert any("Withdraw" in x for x in check.problems)

    def test_read_only_key_is_refused(self):
        p = passport(readOnly=1)
        check = cr.verify(cr.Credentials("ABCD1234", "s"), client=FakeBybit(p))
        assert not check.ok
        assert any("только для чтения" in x for x in check.problems)

    def test_key_without_trade_rights_is_refused(self):
        p = passport(permissions={"Wallet": ["AccountTransfer"]})
        check = cr.verify(cr.Credentials("ABCD1234", "s"), client=FakeBybit(p))
        assert not check.ok and not check.can_trade

    def test_missing_ip_binding_warns_but_passes(self):
        p = passport(ips=["*"])
        check = cr.verify(cr.Credentials("ABCD1234", "s"), client=FakeBybit(p))
        assert check.ok
        assert any("не привязан к IP" in w for w in check.warnings)

    def test_expiring_key_warns(self):
        p = passport(deadlineDay=3)
        check = cr.verify(cr.Credentials("ABCD1234", "s"), client=FakeBybit(p))
        assert check.expires_in_days == 3
        assert any("истекает" in w for w in check.warnings)

    def test_mainnet_says_so(self):
        check = cr.verify(cr.Credentials("ABCD1234", "s", testnet=False),
                          client=FakeBybit(passport()))
        assert check.network == "MAINNET"
        assert any("БОЕВОЙ" in w for w in check.warnings)

    def test_rejected_key_names_the_other_network(self):
        """Самая частая ошибка ввода — ключ не от той сети.

        Биржа на неё отвечает тем же «неверный ключ», что и на опечатку,
        поэтому подсказку даём мы: сеть известна нам, а не бирже.
        """
        cl = FakeBybit(passport(), ret_code=10003)
        check = cr.verify(cr.Credentials("ABCD1234", "s", testnet=True),
                          client=cl)
        assert not check.ok
        assert "MAINNET" in check.problems[0]

    def test_ip_mismatch_is_named_precisely(self):
        cl = FakeBybit(passport(), ret_code=10010)
        check = cr.verify(cr.Credentials("ABCD1234", "s"), client=cl)
        assert not check.ok
        assert "IP" in check.problems[0]

    def test_empty_credentials_do_not_call_exchange(self):
        check = cr.verify(cr.Credentials())
        assert not check.ok and check.problems

    def test_injected_client_is_not_closed(self):
        """Чужой клиент закрывает тот, кто его открыл."""
        cl = FakeBybit(passport())
        cr.verify(cr.Credentials("ABCD1234", "s"), client=cl)
        assert cl.closed is False

    def test_no_secret_in_the_protocol(self, tmp_path: Path):
        cl = FakeBybit(passport())
        check = cr.verify(cr.Credentials("ABCD1234", "top-secret"), client=cl)
        assert "top-secret" not in str(check.as_dict())


class TestStatus:
    def test_protocol_of_another_key_is_dropped(self, tmp_path: Path,
                                                monkeypatch):
        """Ключ сменили — старый протокол проверки к нему не относится.

        Иначе панель покажет зелёную галочку от прошлого ключа над
        новым, ещё не проверенным.
        """
        monkeypatch.delenv(cr.ENV_KEY, raising=False)
        monkeypatch.delenv(cr.ENV_SECRET, raising=False)
        env = tmp_path / "ops" / ".env"
        cr.save(cr.Credentials("AAAA1111", "s1"), env)
        cr.remember(cr.verify(cr.Credentials("AAAA1111", "s1"),
                              client=FakeBybit(passport())), tmp_path)
        assert cr.status(env, tmp_path)["verified"] is True

        cr.save(cr.Credentials("BBBB2222", "s2"), env)
        st = cr.status(env, tmp_path)
        assert st["connected"] is True
        assert st["verified"] is False and st["last_check"] is None

    def test_not_connected(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv(cr.ENV_KEY, raising=False)
        monkeypatch.delenv(cr.ENV_SECRET, raising=False)
        st = cr.status(tmp_path / "ops" / ".env", tmp_path)
        assert st["connected"] is False and st["verified"] is False
