# -*- coding: utf-8 -*-
"""Тесты валидатора tools/registry_check.py (схема реестра, ПВ-03, Д2).

Запуск: uv run python -m unittest discover -s tests -v
Испытательные примеры — из wrk/standard/registry/examples/; схемы — из
wrk/standard/registry/schema/. Тесты не изменяют материалы проекта.
"""

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = REPO_ROOT / "wrk" / "standard" / "registry" / "schema"
EXAMPLES_DIR = REPO_ROOT / "wrk" / "standard" / "registry" / "examples"

import yaml
from jsonschema import Draft202012Validator


def load_module():
    spec = importlib.util.spec_from_file_location(
        "registry_check_under_test", REPO_ROOT / "tools" / "registry_check.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SchemaMetaTests(unittest.TestCase):
    """Схемы синтаксически корректны и покрывают четыре типа записей."""

    def test_schemas_are_valid_draft202012(self):
        for name in (
            "requirement.schema.yaml",
            "asis-application.schema.yaml",
            "standard-release.schema.yaml",
            "verification.schema.yaml",
        ):
            with self.subTest(schema=name):
                with (SCHEMA_DIR / name).open(encoding="utf-8") as fh:
                    schema = yaml.safe_load(fh)
                Draft202012Validator.check_schema(schema)


class SelftestSuite(unittest.TestCase):
    """Положительные примеры проходят; отрицательные отвергаются."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def test_positive_examples_validate(self):
        files = sorted((EXAMPLES_DIR / "positive").glob("*.yaml"))
        self.assertGreater(len(files), 0)
        for path in files:
            with self.subTest(file=path.name):
                ok, msg = self.mod.validate_file(path)
                self.assertTrue(ok, msg)

    def test_negative_examples_rejected(self):
        files = sorted((EXAMPLES_DIR / "negative").glob("*.yaml"))
        self.assertGreater(len(files), 0)
        for path in files:
            with self.subTest(file=path.name):
                ok, _msg = self.mod.validate_file(path)
                self.assertFalse(ok)

    def test_three_norm_kinds_present(self):
        """Испытание § 6 п. 4 плана: паспортная, условная, с допустимой неизвестностью."""
        names = {p.name for p in (EXAMPLES_DIR / "positive").glob("*.yaml")}
        for expected in (
            "requirement-cdm-001.yaml",
            "requirement-ldm-034.yaml",
            "requirement-cdm-015.yaml",
        ):
            self.assertIn(expected, names)

    def test_cli_selftest_exit_zero(self):
        rc = self.mod.main(["selftest"])
        self.assertEqual(rc, 0)

    def test_cli_check_negative_file_exit_one(self):
        bad = EXAMPLES_DIR / "negative" / "req-bad-modality.yaml"
        rc = self.mod.main(["check", "--file", str(bad)])
        self.assertEqual(rc, 1)


class SemanticsTests(unittest.TestCase):
    """Точечные семантические правила схем (неопределённость, режимные связи)."""

    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()

    def _validate(self, doc):
        import io as _io
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as fh:
            yaml.safe_dump(doc, fh, allow_unicode=True)
            path = Path(fh.name)
        try:
            return self.mod.validate_file(path)
        finally:
            path.unlink()

    def test_undefined_mode_only_in_draft(self):
        ok, msg = self._validate(
            {
                "schema_version": 1,
                "id": "LDM-099",
                "revision": 1,
                "formulation": "Формулировка.",
                "modality": "required",
                "applies_to": {"mode": ["undefined"]},
                "status": "draft",
            }
        )
        self.assertTrue(ok, msg)

    def test_mixed_undefined_rejected_even_in_draft(self):
        ok, _ = self._validate(
            {
                "schema_version": 1,
                "id": "LDM-099",
                "revision": 1,
                "formulation": "Формулировка.",
                "modality": "required",
                "applies_to": {"mode": ["to-be", "undefined"]},
                "status": "draft",
            }
        )
        self.assertFalse(ok)

    def test_adapted_requires_link_and_rationale(self):
        ok, _ = self._validate(
            {
                "schema_version": 1,
                "record": "asis_application",
                "standard_release": "0.2",
                "requirement": {"id": "LDM-031", "revision": 1},
                "way": "adapted",
                "relation_type": "adapts_for_mode",
            }
        )
        self.assertFalse(ok)

    def test_not_applied_requires_rationale(self):
        """REV-001/std-002: not_applied без обоснования и без связей — отвергается.

        Отдельного YAML-файла нет: кандидат прогона не может добавлять файлы
        к манифесту базы; правило покрыто этим тестом (эквивалент negative-примера).
        """
        ok, _ = self._validate(
            {
                "schema_version": 1,
                "record": "asis_application",
                "standard_release": "0.2",
                "requirement": {"id": "CDM-038", "revision": 1},
                "way": "not_applied",
            }
        )
        self.assertFalse(ok)

    def test_verification_procedure_nullable(self):
        """REV-002/std-002: procedure: null допустим до наполнения прил. Ж (Д7)."""
        ok, msg = self._validate(
            {
                "schema_version": 1,
                "record": "verification",
                "id": "V-000125",
                "requirement": {"id": "CDM-015", "revision": 1},
                "standard_release": "0.2",
                "procedure": None,
                "scope": "model",
                "object": {"model": "M-042", "version": 5},
                "result": "not_checked",
                "checked_by": {
                    "initiator": "тест",
                    "executor": "тест",
                    "date": "2026-10-11",
                },
            }
        )
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()
