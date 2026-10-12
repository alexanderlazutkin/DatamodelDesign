# -*- coding: utf-8 -*-
"""Тесты инструмента навигации tools/nav.py (Р-041: якоря, linkify, check).

Запуск: uv run python -m unittest discover -s tests -v
Тесты работают на временных копиях комплекта (tmp); материалы проекта не изменяются.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "nav_under_test", REPO_ROOT / "tools" / "nav.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_kit(root: Path) -> None:
    """Минимальный комплект, отражающий структуру wrk/standard/."""
    std = root / "wrk" / "standard"
    app = std / "appendices"
    reg = std / "registry"
    app.mkdir(parents=True)
    reg.mkdir(parents=True)

    (std / "Стандарт.md").write_text(
        "# Стандарт\n"
        "\n"
        "## 1. Первый раздел\n"
        "\n"
        "**LDM-040 (обязательное).** Формулировка требования.\n"
        "\n"
        "**LDM-041 (обязательное).** Формулировка со ссылкой на LDM-040 внутри определения.\n"
        "\n"
        "Упоминание LDM-040 и CDM-043/LDM-039, диапазон LDM-040…046, перечень PR-004/005.\n"
        "\n"
        "См. прил. И.4 и прил. Ж; повторно прил. И.4 в том же разделе.\n"
        "\n"
        "| Колонка | Значение |\n"
        "|---|---|\n"
        "| LDM-040 в таблице | не линкуется |\n"
        "\n"
        "## 2. Второй раздел\n"
        "\n"
        "LDM-040 снова — первое упоминание в новом разделе, T-001 тоже.\n"
        "\n"
        "Текст перед определением в середине абзаца: **GEN-005 (обязательное).** Формулировка.\n",
        encoding="utf-8",
    )

    (std / "Регламент AS-IS.md").write_text(
        "# Регламент AS-IS\n"
        "\n"
        "## 4.6. Специальный порядок\n"
        "\n"
        "**CDM-043 (обязательное).** Формулировка, размещённая в регламенте.\n"
        "\n"
        "Ссылка на LDM-040 стандарта и прил. Г.6.\n",
        encoding="utf-8",
    )

    (app / "Ж Контрольные листы и процедуры приёмки.md").write_text(
        "# Приложение Ж. Контрольные листы\n"
        "\n"
        "## Ж.10. Реестр контрольных процедур\n"
        "\n"
        "| PR | Название | Статус |\n"
        "|---|---|---|\n"
        "| PR-001 | Проверка | не испытана |\n"
        "\n"
        "**PR-001. Проверка.** Назначение: метаданные; вход — прил. Г.6. Статус: не испытана.\n",
        encoding="utf-8",
    )

    (app / "И Профиль классификации ИОД.md").write_text(
        "# Приложение И. Профиль классификации ИОД\n"
        "\n"
        "## И.4. Минимальный объём для пилота\n"
        "\n"
        "Текст И.4.\n"
        "\n"
        "## И.5. Связи с разделами\n"
        "\n"
        "Текст И.5, см. LDM-040.\n",
        encoding="utf-8",
    )

    (app / "Г Минимальные метаданные.md").write_text(
        "# Приложение Г. Минимальные метаданные\n"
        "\n"
        "## Г.6. Свидетельства\n"
        "\n"
        "Текст Г.6.\n",
        encoding="utf-8",
    )

    (reg / "terms-draft.md").write_text(
        "# Словарь терминов\n"
        "\n"
        "| ID | Термин | Область | Резюме | Основание |\n"
        "|---|---|---|---|---|\n"
        "| T-001 | Домен | ЛМД | Резюме | С04; LDM-027 |\n"
        "| T-002 | Модель | управление | Резюме | С04 |\n"
        "\n"
        "Термин T-001 в тексте словаря.\n",
        encoding="utf-8",
    )


class NavFixtureTest(unittest.TestCase):
    """anchors → linkify → check на временном комплекте."""

    @classmethod
    def setUpClass(cls):
        cls.nav = load_module()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        make_kit(cls.root)
        cls.nav.cmd_anchors(cls.root)
        cls.nav.cmd_linkify(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def read(self, rel: str) -> str:
        return (self.root / "wrk" / "standard" / rel).read_text(encoding="utf-8")

    # ---------- anchors ----------

    def test_definition_line_not_linked(self):
        line = [
            l for l in self.read("Стандарт.md").splitlines() if l.endswith("^LDM-041")
        ][0]
        self.assertNotIn("[[", line)  # дословность: строка-определение без ссылок

    def test_anchor_requirement_paragraph(self):
        self.assertIn(
            "**LDM-040 (обязательное).** Формулировка требования. ^LDM-040",
            self.read("Стандарт.md"),
        )

    def test_anchor_requirement_in_regulation(self):
        self.assertIn(
            "размещённая в регламенте. ^CDM-043", self.read("Регламент AS-IS.md")
        )

    def test_anchor_term_row(self):
        self.assertIn(
            "| T-001 | Домен | ЛМД | Резюме | С04; LDM-027 ^T-001 |",
            self.read("registry/terms-draft.md"),
        )

    def test_anchor_pr_card(self):
        self.assertIn(
            "Статус: не испытана. ^PR-001",
            self.read("appendices/Ж Контрольные листы и процедуры приёмки.md"),
        )

    def test_anchor_inline_definition(self):
        self.assertIn("Формулировка. ^GEN-005", self.read("Стандарт.md"))

    def test_anchors_idempotent(self):
        before = self.read("Стандарт.md")
        self.nav.cmd_anchors(self.root)
        self.assertEqual(before, self.read("Стандарт.md"))

    # ---------- linkify ----------

    def test_first_mention_linked_repeat_plain(self):
        text = self.read("Стандарт.md")
        self.assertEqual(
            text.count("[[Стандарт#^LDM-040|LDM-040]]"), 2
        )  # разделы 1 и 2

    def test_slash_enum_not_linked(self):
        self.assertNotIn("|CDM-043", self.read("Стандарт.md").split("## 2.")[0])
        self.assertIn("CDM-043/LDM-039", self.read("Стандарт.md"))

    def test_range_not_linked(self):
        self.assertIn("LDM-040…046", self.read("Стандарт.md"))

    def test_table_not_linked(self):
        self.assertIn("| LDM-040 в таблице | не линкуется |", self.read("Стандарт.md"))

    def test_appendix_point_link(self):
        self.assertIn(
            "[[И Профиль классификации ИОД#И.4. Минимальный объём для пилота|прил. И.4]]",
            self.read("Стандарт.md"),
        )

    def test_appendix_doc_link_and_density(self):
        text = self.read("Стандарт.md")
        self.assertIn("[[Ж Контрольные листы и процедуры приёмки|прил. Ж]]", text)
        self.assertEqual(text.count("прил. И.4]]"), 1)  # повтор в разделе — плоский

    def test_cross_file_term_link(self):
        self.assertIn("[[terms-draft#^T-001|T-001]]", self.read("Стандарт.md"))

    def test_pr_self_definition_not_linked(self):
        text = self.read("appendices/Ж Контрольные листы и процедуры приёмки.md")
        self.assertIn("**PR-001. Проверка.**", text)
        self.assertNotIn(
            "[[Ж Контрольные листы и процедуры приёмки#^PR-001",
            text.split("Назначение")[0],
        )

    def test_regulation_links_to_standard(self):
        text = self.read("Регламент AS-IS.md")
        self.assertIn("[[Стандарт#^LDM-040|LDM-040]]", text)
        self.assertIn("[[Г Минимальные метаданные#Г.6. Свидетельства|прил. Г.6]]", text)

    # ---------- check ----------

    def test_check_green(self):
        errors = self.nav.cmd_check(self.root)
        self.assertEqual(errors, [])

    def test_check_broken_link(self):
        std = self.root / "wrk" / "standard" / "Стандарт.md"
        original = std.read_text(encoding="utf-8")
        std.write_text(original.replace("#^LDM-040|", "#^LDM-999|"), encoding="utf-8")
        try:
            errors = self.nav.cmd_check(self.root)
            self.assertTrue(errors and "LDM-999" in errors[0])
        finally:
            std.write_text(original, encoding="utf-8")

    def test_check_duplicate_anchor(self):
        reg = self.root / "wrk" / "standard" / "Регламент AS-IS.md"
        original = reg.read_text(encoding="utf-8")
        reg.write_text(original.replace("^CDM-043", "^LDM-040"), encoding="utf-8")
        try:
            errors = self.nav.cmd_check(self.root)
            self.assertTrue(any("дубл" in e or "LDM-040" in e for e in errors))
        finally:
            reg.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()


class GuardNormalizeTest(unittest.TestCase):
    """Нормализация: свёртка пробелов, канонизация разделителей, код не трогаем."""

    @classmethod
    def setUpClass(cls):
        cls.nav = load_module()

    def test_padding_collapsed(self):
        a = self.nav.normalize_md("| Ячейка        | Значение   |\n| текст         | ещё        |")
        b = self.nav.normalize_md("| Ячейка | Значение |\n| текст | ещё |")
        self.assertEqual(a, b)

    def test_separator_canonicalized(self):
        a = self.nav.normalize_md("| Реквизит | Значение |\n| -------------------------- | --------- |\n| a | b |")
        b = self.nav.normalize_md("| Реквизит | Значение |\n|---|---|\n| a | b |")
        self.assertEqual(a, b)

    def test_trailing_spaces_ignored(self):
        a = self.nav.normalize_md("текст строки   \nвторая\t")
        b = self.nav.normalize_md("текст строки\nвторая")
        self.assertEqual(a, b)

    def test_code_block_untouched(self):
        # значимые пробелы в коде не сворачиваются
        a = self.nav.normalize_md("```\n  отступ  два  пробела\n```")
        self.assertIn("  отступ  два  пробела", a)

    def test_content_change_detected(self):
        a = self.nav.normalize_md("| Ячейка | Значение |")
        b = self.nav.normalize_md("| Ячейка | Другое |")
        self.assertNotEqual(a, b)

    def test_inline_code_spaces_kept(self):
        a = self.nav.normalize_md("строка с `кодом  из  двух` пробелами")
        self.assertIn("`кодом  из  двух`", a)


@unittest.skipUnless(
    __import__("subprocess").run(["git", "--version"], capture_output=True).returncode == 0,
    "git недоступен",
)
class GuardRepoTest(unittest.TestCase):
    """guard: whitespace-артефакт откатывается, содержательное изменение остаётся."""

    @classmethod
    def setUpClass(cls):
        import subprocess

        cls.nav = load_module()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        make_kit(cls.root)
        subprocess.run(["git", "init", "-q"], cwd=cls.root, check=True)
        subprocess.run(
            ["git", "add", "wrk"],
            cwd=cls.root, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "kit"],
            cwd=cls.root, check=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_artifact_reverted(self):
        import subprocess

        f = self.root / "wrk" / "standard" / "Стандарт.md"
        original = f.read_text(encoding="utf-8")
        f.write_text(
            original.replace("| Колонка | Значение |", "| Колонка          | Значение |")
                      .replace("|---|---|", "| ------------- | --------- |"),
            encoding="utf-8",
        )
        rc = self.nav.cmd_guard(self.root)
        self.assertEqual(subprocess.run(["git", "-C", str(self.root), "status", "--porcelain", "wrk"], capture_output=True).stdout.decode().strip(), "")
        self.assertEqual(f.read_text(encoding="utf-8"), original)
        self.assertEqual(rc, 0)

    def test_content_change_kept(self):
        f = self.root / "wrk" / "standard" / "Стандарт.md"
        original = f.read_text(encoding="utf-8")
        try:
            f.write_text(original.replace("Второй раздел", "Второй раздел (правка)"), encoding="utf-8")
            rc = self.nav.cmd_guard(self.root)
            self.assertEqual(rc, 1)  # содержательное изменение осталось
            self.assertIn("(правка)", f.read_text(encoding="utf-8"))
        finally:
            f.write_text(original, encoding="utf-8")
