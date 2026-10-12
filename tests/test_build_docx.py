# -*- coding: utf-8 -*-
"""Тесты сборщика единого .docx tools/build_docx.py (Р-041: навигация закладками).

Запуск: uv run --with jsonschema --with python-docx python -m unittest discover -s tests
Тесты работают на временном комплекте (tmp); материалы проекта не изменяются.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

try:
    import docx  # noqa: F401

    HAS_PYTHON_DOCX = True
except ImportError:
    HAS_PYTHON_DOCX = False

if HAS_PYTHON_DOCX:
    from docx import Document

    from tests.test_nav import load_module as load_nav, make_kit

    def load_builder():
        import sys

        spec = importlib.util.spec_from_file_location(
            "build_docx_under_test", REPO_ROOT / "tools" / "build_docx.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["build_docx_under_test"] = mod  # нужно для dataclasses
        spec.loader.exec_module(mod)
        return mod


@unittest.skipUnless(
    HAS_PYTHON_DOCX, "python-docx не установлен (uv run --with python-docx)"
)
class BuildDocxTest(unittest.TestCase):
    """Сборка tmp-комплекта и верификация навигации в .docx."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.nav = load_nav()
        cls.root = Path(cls.tmp.name)
        make_kit(cls.root)
        cls.nav.cmd_anchors(cls.root)
        cls.nav.cmd_linkify(cls.root)
        # дополнения: код-блок, список, цитата, внешняя ссылка
        std = cls.root / "wrk" / "standard" / "Стандарт.md"
        std.write_text(
            std.read_text(encoding="utf-8")
            + "\n```\ncode LDM-040 fence\n```\n"
            + "\n- пункт один;\n- пункт два.\n\n1. нумерованный;\n2. второй.\n"
            + "\n> Цитата-примечание.\n\n[Внешний](https://example.com) и хвост.\n",
            encoding="utf-8",
        )
        cls.builder = load_builder()
        cls.out = Path(cls.tmp.name) / "kit.docx"
        cls.builder.build(
            cls.root, cls.out, title="Тестовый комплект", version="0.1-тест"
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def doc_xml_items(self, tag):
        doc = Document(str(self.out))
        return list(doc.element.iter(tag))

    def test_file_created(self):
        self.assertTrue(self.out.exists() and self.out.stat().st_size > 5000)

    def test_verify_no_problems(self):
        self.assertEqual(self.builder.verify_docx(self.out), [])

    def test_bookmarks_for_ids(self):
        names = {
            el.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}name")
            for el in self.doc_xml_items(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}bookmarkStart"
            )
        }
        for bm in ("a_T_001", "a_LDM_040", "a_PR_001", "a_CDM_043", "a_GEN_005"):
            self.assertIn(bm, names)

    def test_internal_hyperlinks_resolve(self):
        W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        anchors = {
            el.get(W + "anchor")
            for el in self.doc_xml_items(W + "hyperlink")
            if el.get(W + "anchor")
        }
        names = {el.get(W + "name") for el in self.doc_xml_items(W + "bookmarkStart")}
        self.assertTrue(anchors, "внутренних гиперссылок нет")
        self.assertEqual(anchors - names, set())

    def test_external_hyperlink_present(self):
        W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}hyperlink"
        doc = Document(str(self.out))
        rels = doc.part.rels
        ext = [
            r for r in rels.values() if r.is_external and "example.com" in r.target_ref
        ]
        self.assertTrue(ext)

    def test_toc_links_to_docs(self):
        text = "\n".join(p.text for p in Document(str(self.out)).paragraphs)
        self.assertIn("Содержание", text)
        self.assertIn("Стандарт", text)


if __name__ == "__main__":
    unittest.main()
