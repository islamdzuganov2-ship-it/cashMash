"""Подмножество pydantic для сред, где pydantic поставить нельзя.

Зачем это существует. Ядро pydantic 2 написано на Rust, и колеса под
Android не существует — ни в индексе Chaquopy, ни на PyPI. Выбор стоял
между «переписать конфиг на dataclass» и «дать pydantic замену ровно на
том участке, который конфиг использует». Выбрано второе: первый вариант
менял бы поведение там, где робот торгует по-настоящему — на компьютере, —
ради среды, где он только наблюдает.

Правило модуля: на машине с pydantic он не подключается вообще.
`config.py` импортирует настоящий pydantic, пока тот есть, и переходит
сюда только при `ImportError`. Поэтому расхождение возможно ровно одно —
между телефоном и компьютером, — и оно закрыто `tests/test_minimodel.py`:
тест сверяет обе реализации на одном конфиге, включая отпечаток
риск-профиля. Отпечаток считается по JSON и потому ловит любое различие
в сериализации, а не только в значениях.

Что поддержано: поля `str/int/float/bool/Decimal/list[str]`, вложенные
модели, `Field(default_factory=...)`, `field_validator`,
`model_validator(mode="after")`, `model_validate`, `model_dump_json`.
Чего нет: `Optional`, `Union`, произвольные типы, `mode="before"`,
JSON Schema. Понадобится — дописывать сюда, а не обходить в конфиге.
"""

from __future__ import annotations

import json
import typing
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


class _Unset:
    """Отсутствие значения, отличимое от None."""

    def __repr__(self) -> str:            # pragma: no cover - для отладки
        return "<unset>"


_UNSET = _Unset()

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


class FieldInfo:
    __slots__ = ("default", "default_factory")

    def __init__(self, default: Any = _UNSET,
                 default_factory: Callable[[], Any] | None = None) -> None:
        self.default = default
        self.default_factory = default_factory

    def make_default(self) -> Any:
        if self.default_factory is not None:
            return self.default_factory()
        return self.default


def Field(default: Any = _UNSET, *,
          default_factory: Callable[[], Any] | None = None,
          **_ignored: Any) -> Any:
    """Описание поля.

    Лишние именованные аргументы pydantic (описания, границы) молча
    игнорируются: конфиг ими не пользуется, а падать на них значило бы
    ломать совместимость из-за того, чего в файле нет.
    """
    return FieldInfo(default, default_factory)


def field_validator(*names: str, **_ignored: Any) -> Callable[[Any], Any]:
    """Проверка одного поля. Значение возвращается, а не правится на месте."""

    def deco(fn: Any) -> Any:
        raw = fn.__func__ if isinstance(fn, (classmethod, staticmethod)) else fn
        raw._mm_fields = names
        return classmethod(raw)

    return deco


def model_validator(*, mode: str = "after",
                    **_ignored: Any) -> Callable[[Any], Any]:
    """Проверка модели целиком.

    Поддержан только режим `after`: он единственный, которым пользуется
    конфиг, а `before` потребовал бы другой модели данных — словаря
    вместо объекта. Молча принять его и выполнить как `after` было бы
    хуже отказа: проверка сработала бы не в тот момент.
    """
    if mode != "after":
        raise NotImplementedError(
            f"model_validator(mode={mode!r}) в замене pydantic не реализован")

    def deco(fn: Any) -> Any:
        raw = fn.__func__ if isinstance(fn, (classmethod, staticmethod)) else fn
        raw._mm_model = True
        return raw

    return deco


def _coerce(name: str, ann: Any, value: Any) -> Any:
    """Привести значение к типу поля по правилам, совпадающим с pydantic.

    Совпадение важнее строгости: конфиг, принятый на компьютере, обязан
    быть принят и на телефоне — иначе одна и та же строка YAML означала бы
    разное на разных устройствах.
    """
    origin = typing.get_origin(ann)
    if origin is list:
        if not isinstance(value, (list, tuple)):
            raise ValueError(
                f"{name}: ожидался список, получено {type(value).__name__}")
        args = typing.get_args(ann)
        item_ann = args[0] if args else str
        return [_coerce(f"{name}[{i}]", item_ann, v)
                for i, v in enumerate(value)]

    if isinstance(ann, type) and issubclass(ann, BaseModel):
        if isinstance(value, ann):
            return value
        if isinstance(value, dict):
            return ann.model_validate(value)
        raise ValueError(
            f"{name}: ожидался раздел, получено {type(value).__name__}")

    if ann is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in _TRUE | _FALSE:
            return value.lower() in _TRUE
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError(f"{name}: ожидалось да/нет, получено {value!r}")

    if ann is int:
        if isinstance(value, bool):
            raise ValueError(f"{name}: ожидалось целое, получено {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise ValueError(f"{name}: ожидалось целое, получено {value!r}")

    if ann is float:
        if isinstance(value, bool):
            raise ValueError(f"{name}: ожидалось число, получено {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ValueError(f"{name}: ожидалось число, получено {value!r}")

    if ann is Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, bool):
            raise ValueError(f"{name}: ожидалось число, получено {value!r}")
        if isinstance(value, (int, float, str)):
            # Через str, а не Decimal(float): pydantic поступает так же,
            # и 0.3 обязан стать Decimal("0.3"), а не двоичным хвостом
            # вида 0.29999999999999998889776975374843...
            try:
                return Decimal(str(value).strip())
            except InvalidOperation:
                pass
        raise ValueError(f"{name}: ожидалось число, получено {value!r}")

    if ann is str:
        if isinstance(value, str):
            return value
        raise ValueError(
            f"{name}: ожидалась строка, получено {type(value).__name__}")

    if ann is Any:
        return value

    raise NotImplementedError(
        f"{name}: тип {ann!r} в замене pydantic не поддержан")


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return {k: _to_jsonable(getattr(value, k)) for k in value._mm_fields_}
    if isinstance(value, Decimal):
        # pydantic пишет Decimal строкой — иначе точность теряется
        # в первом же разборе JSON.
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


class BaseModel:
    """Модель с проверкой типов и валидаторами.

    Поля объявляются аннотациями класса, как в pydantic; порядок
    объявления сохраняется и задаёт порядок ключей в JSON.
    """

    _mm_fields_: dict[str, Any] = {}
    _mm_defaults_: dict[str, FieldInfo] = {}
    _mm_field_validators_: dict[str, list[Callable[..., Any]]] = {}
    _mm_model_validators_: list[Callable[..., Any]] = []

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        hints = typing.get_type_hints(cls)
        fields: dict[str, Any] = {}
        defaults: dict[str, FieldInfo] = {}
        for name in cls.__dict__.get("__annotations__", {}):
            if name.startswith("_"):
                continue
            fields[name] = hints[name]
            raw = cls.__dict__.get(name, _UNSET)
            defaults[name] = raw if isinstance(raw, FieldInfo) else FieldInfo(raw)
        cls._mm_fields_ = fields
        cls._mm_defaults_ = defaults

        fvals: dict[str, list[Callable[..., Any]]] = {}
        mvals: list[Callable[..., Any]] = []
        for attr in cls.__dict__.values():
            fn = (attr.__func__
                  if isinstance(attr, (classmethod, staticmethod)) else attr)
            for field in getattr(fn, "_mm_fields", ()):
                fvals.setdefault(field, []).append(fn)
            if getattr(fn, "_mm_model", False):
                mvals.append(fn)
        cls._mm_field_validators_ = fvals
        cls._mm_model_validators_ = mvals

        # Значения по умолчанию остаются атрибутами класса и мешали бы:
        # getattr на незаполненном поле возвращал бы FieldInfo вместо
        # значения, и ошибка всплыла бы далеко от места.
        for name in fields:
            if name in cls.__dict__:
                delattr(cls, name)

    def __init__(self, **data: Any) -> None:
        for name, ann in self._mm_fields_.items():
            if name in data:
                value = _coerce(name, ann, data[name])
            else:
                value = self._mm_defaults_[name].make_default()
                if isinstance(value, _Unset):
                    raise ValueError(f"{name}: обязательное поле не задано")
            for check in self._mm_field_validators_.get(name, ()):
                value = check(type(self), value)
            object.__setattr__(self, name, value)

        for check in self._mm_model_validators_:
            result = check(self)
            if result is not None and result is not self:
                raise ValueError(
                    f"{type(self).__name__}: валидатор модели вернул "
                    f"посторонний объект")

    @classmethod
    def model_validate(cls, data: Any) -> "BaseModel":
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            raise ValueError(f"{cls.__name__}: ожидался раздел конфига")
        # Лишние ключи игнорируются — как в pydantic по умолчанию.
        known = {k: v for k, v in data.items() if k in cls._mm_fields_}
        return cls(**known)

    def model_dump(self) -> dict[str, Any]:
        return {k: _to_jsonable(getattr(self, k)) for k in self._mm_fields_}

    def model_dump_json(self) -> str:
        # Разделители без пробелов и без экранирования не-ASCII — байт
        # в байт как у pydantic, иначе отпечаток риск-профиля на телефоне
        # и на компьютере разойдётся, и журналы перестанут сходиться.
        return json.dumps(self.model_dump(), separators=(",", ":"),
                          ensure_ascii=False)

    def __repr__(self) -> str:            # pragma: no cover - для отладки
        inner = ", ".join(f"{k}={getattr(self, k)!r}" for k in self._mm_fields_)
        return f"{type(self).__name__}({inner})"

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return NotImplemented
        return all(getattr(self, k) == getattr(other, k)
                   for k in self._mm_fields_)
