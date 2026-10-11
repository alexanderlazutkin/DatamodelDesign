#!/usr/bin/env python3
"""Локальный валидатор записей реестра требований (контур А2, этап Д2).

Проверяет YAML-записи против схем JSON Schema draft 2020-12 из
wrk/standard/registry/schema/. Код 0 — все проверки формально выполнены;
это не утверждение содержания записей.

Команды:
  check --file F [...]   проверить указанные файлы записей
  selftest               прогнать испытательный набор: положительные примеры
                         обязаны проходить, отрицательные — отвергаться
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "wrk" / "standard" / "registry" / "schema"
EXAMPLES_DIR = ROOT / "wrk" / "standard" / "registry" / "examples"

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_INPUT = 2

SCHEMAS = {
    "requirement": "requirement.schema.yaml",
    "asis_application": "asis-application.schema.yaml",
    "standard_release": "standard-release.schema.yaml",
    "verification": "verification.schema.yaml",
}
RECORD_FIELD = {name: name for name in SCHEMAS if name != "requirement"}

_validators: dict[str, Draft202012Validator] = {}


def load_validator(kind: str) -> Draft202012Validator:
    if kind not in _validators:
        path = SCHEMA_DIR / SCHEMAS[kind]
        if not path.is_file():
            raise FileNotFoundError(f"схема не найдена: {path}")
        with path.open(encoding="utf-8") as fh:
            schema = yaml.safe_load(fh)
        Draft202012Validator.check_schema(schema)
        _validators[kind] = Draft202012Validator(schema)
    return _validators[kind]


def detect_kind(doc: object, source: Path) -> str:
    if not isinstance(doc, dict):
        raise ValueError(f"{source}: запись должна быть отображением (mapping)")
    record = doc.get("record")
    if record is None:
        return "requirement"
    if record not in RECORD_FIELD:
        raise ValueError(f"{source}: неизвестный record: {record!r}")
    return RECORD_FIELD[record]


def validate_file(path: Path) -> tuple[bool, str]:
    """Возвращает (пройдено, сообщение)."""
    try:
        with path.open(encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        return False, f"{path.name}: YAML не разобран: {exc}"
    try:
        kind = detect_kind(doc, path)
    except ValueError as exc:
        return False, str(exc)
    errors = sorted(
        load_validator(kind).iter_errors(doc), key=lambda e: list(e.absolute_path)
    )
    if errors:
        first = errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<корень>"
        return False, f"{path.name}: {first.message} (в {loc})"
    return True, f"{path.name}: ok ({kind})"


def cmd_check(args: argparse.Namespace) -> int:
    failures = 0
    for raw in args.file:
        path = Path(raw)
        if not path.is_file():
            print(f"ФАЙЛ НЕ НАЙДЕН: {path}")
            return EXIT_INPUT
        ok, msg = validate_file(path)
        print(("ПРОЙДЕНО  " if ok else "ОТВЕРГНУТО ") + msg)
        if not ok:
            failures += 1
    return EXIT_OK if failures == 0 else EXIT_VIOLATION


def cmd_selftest(_: argparse.Namespace) -> int:
    bad = 0
    pos_dir = EXAMPLES_DIR / "positive"
    neg_dir = EXAMPLES_DIR / "negative"
    for d in (pos_dir, neg_dir):
        if not d.is_dir():
            print(f"КАТАЛОГ НЕ НАЙДЕН: {d}")
            return EXIT_INPUT
    print("Положительные примеры (обязаны проходить):")
    for path in sorted(pos_dir.glob("*.yaml")):
        ok, msg = validate_file(path)
        print(("  [ok]   " if ok else "  [FAIL] ") + msg)
        if not ok:
            bad += 1
    print("Отрицательные примеры (обязаны отвергаться):")
    for path in sorted(neg_dir.glob("*.yaml")):
        ok, msg = validate_file(path)
        if ok:
            print(f"  [FAIL] {path.name}: ПРОШЛО, хотя обязано отвергаться")
            bad += 1
        else:
            reason = msg.split(": ", 1)[-1]
            print(f"  [ok]   {path.name}: отвергнуто ({reason})")
    total = len(list(pos_dir.glob("*.yaml"))) + len(list(neg_dir.glob("*.yaml")))
    print(f"Испытательный набор: {total} файлов, нарушений согласованности: {bad}")
    return EXIT_OK if bad == 0 else EXIT_VIOLATION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="проверить указанные файлы записей")
    p_check.add_argument("--file", nargs="+", required=True, help="пути к YAML-записям")
    p_check.set_defaults(func=cmd_check)

    p_self = sub.add_parser("selftest", help="испытательный набор схем (Д2, ПВ-03)")
    p_self.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ОШИБКА ВХОДОВ: {exc}")
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
