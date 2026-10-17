#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/nav.py — навигация нормативного комплекта (Р-041, правила НР-1…НР-5).

Подкоманды:
  anchors  — вставка block-ID якорей (^T-xxx, ^GEN|CDM|LDM-xxx, ^PR-xxx); идемпотентно;
  linkify  — плоские отсылки -> wikilink Obsidian (первое упоминание в разделе ##);
  check    — резолв всех wikilink-ссылок и якорей; код выхода 1 — ошибка приёмки.

Запуск: uv run python tools/nav.py anchors|linkify|check [--root PATH] [--dry]
Стандартная библиотека; зависимости не требуются.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

RU_APPENDIX_ORDER = "АБВГДЕЖИКЛМНПРСТУФ"

REQ_DEF = re.compile(r"^\*\*((?:GEN|CDM|LDM)-\d{3})\b")
PR_DEF = re.compile(r"^\*\*(PR-\d{3})\.")
INLINE_REQ_DEF = re.compile(
    r"\*\*((?:GEN|CDM|LDM)-\d{3})\s*\("
)  # определение в середине абзаца
INLINE_PR_DEF = re.compile(r"\*\*(PR-\d{3})\.\s")
ID_ANY = re.compile(r"\b((?:GEN|CDM|LDM|PR)-\d{3}|T-\d{3})\b")
FENCE = re.compile(r"^```")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
H2 = re.compile(r"^##\s")
TABLE_ROW = re.compile(r"^\s*\|")
SEP_ROW = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
WIKILINK = re.compile(r"\[\[([^\[\]]+?)\]\]")
INLINE_CODE = re.compile(r"`[^`]*`")
PARA_ANCHOR = re.compile(r"\^([A-Za-z][\w-]*)\s*$")
ROW_ANCHOR = re.compile(r"\^([\w-]+)\s*\|\s*$")
ANCHOR_FORMAT = re.compile(r"^(?:T|(?:GEN|CDM|LDM|PR))-\d{3}$")
APPENDIX_REF = re.compile(r"(?:прил\.|приложени[а-яё]*)\s+([А-Ф])(?:\.(\d+))?")
RANGE_AFTER = re.compile(r"^\s*(?:…|–|—|-)\s*(?:\d|[A-ZА-Я]{1,4}-\d)")
RANGE_BEFORE = re.compile(r"(?:…|–|—|/|-)\s*$")


class Doc:
    """Один markdown-документ комплекта."""

    def __init__(self, path: Path, role: str):
        self.path = path
        self.role = role  # standard | regulation | appendix | terms | rules
        self.stem = path.stem
        self.headings: list[str] = []
        self.block_ids: set[str] = set()

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def scan(self) -> None:
        self.headings.clear()
        self.block_ids.clear()
        fenced = False
        for line in self.read().splitlines():
            if FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            m = HEADING.match(line)
            if m:
                self.headings.append(m.group(2))
                continue
            if TABLE_ROW.match(line):
                am = ROW_ANCHOR.search(line)
                if am:
                    self.block_ids.add(am.group(1))
                continue
            am = PARA_ANCHOR.search(line)
            if am:
                self.block_ids.add(am.group(1))


def kit_docs(root: Path) -> list[Doc]:
    """Упорядоченный комплект: Правила, Регламент, прил. А…Ф, словарь, правила."""
    std = root / "wrk" / "standard"
    docs = [
        Doc(std / "Правила.md", "standard"),
        Doc(std / "Регламент AS-IS.md", "regulation"),
    ]
    app_dir = std / "appendices"
    if app_dir.is_dir():
        files = {p.stem.split(" ", 1)[0]: p for p in app_dir.glob("*.md")}
        for letter in RU_APPENDIX_ORDER:
            if letter in files:
                docs.append(Doc(files[letter], "appendix"))
        for key, p in sorted(files.items()):  # нестандартные буквы — в конец
            if key not in RU_APPENDIX_ORDER:
                docs.append(Doc(p, "appendix"))
    docs.append(Doc(std / "Правила оформления и навигации.md", "rules"))
    return [d for d in docs if d.path.exists()]


def mask_code(text: str) -> str:
    """Маскирует inline-code (`...`) символами \x00."""
    masked = list(text)
    for m in INLINE_CODE.finditer(text):
        for i in range(m.start(), m.end()):
            masked[i] = "\x00"
    return "".join(masked)


def mask_all(text: str) -> str:
    """Маскирует inline-code и существующие wikilink (для поиска новых целей)."""
    masked = list(text)
    for rx in (WIKILINK, INLINE_CODE):
        for m in rx.finditer(text):
            for i in range(m.start(), m.end()):
                masked[i] = "\x00"
    return "".join(masked)


def is_plain_boundary(text: str, s: int, e: int) -> bool:
    """Границы упоминания: соседние символы не являются частью слова/перечня."""
    if s > 0 and (text[s - 1].isalnum()):
        return False
    if e < len(text) and text[e].isalnum():
        return False
    return True


def cmd_anchors(root: Path, dry: bool = False) -> None:
    """Вставка block-ID: определения требований, карточки PR, строки словаря."""
    docs = kit_docs(root)
    for d in docs:
        d.scan()
    anchored_in_standard = next(
        (d.block_ids for d in docs if d.role == "standard"), set()
    )
    anchored_global = set().union(*(d.block_ids for d in docs))
    report: dict[str, dict[str, int]] = {}
    for doc in docs:
        lines = doc.read().splitlines()
        out, added, skipped = [], 0, 0
        for line in lines:
            anchor = None
            m = REQ_DEF.match(line) or PR_DEF.match(line)
            if m:
                anchor = m.group(1)
            elif not TABLE_ROW.match(line) and not HEADING.match(line):
                inline = set(INLINE_REQ_DEF.findall(line))
                inline |= set(INLINE_PR_DEF.findall(line))
                inline -= anchored_global  # уже закреплены в других местах
                if len(inline) == 1:
                    anchor = inline.pop()
                elif len(inline) > 1:
                    print(
                        f"anchors: {doc.path.name}: несколько определений в одном абзаце "
                        f"{sorted(inline)} — пропущено"
                    )
            if anchor and doc.role == "regulation" and anchor in anchored_in_standard:
                anchor = None  # каноническое определение — в Правилах
            if anchor and not ANCHOR_FORMAT.match(anchor):
                anchor = None
            if not anchor and line.startswith("| T-"):
                tm = re.match(r"\|\s*(T-\d{3})\s*\|", line)
                anchor = tm.group(1) if tm else None
            if not anchor:
                out.append(line)
                continue
            done = PARA_ANCHOR.search(line) or ROW_ANCHOR.search(
                line
            )  # на строке уже есть любой якорь
            if done or f"^{anchor}" in line:
                skipped += 1
                out.append(line)
                continue
            if line.rstrip().endswith("|"):
                core = line.rstrip()[:-1].rstrip()
                out.append(f"{core} ^{anchor} |")
            else:
                out.append(f"{line.rstrip()} ^{anchor}")
            added += 1
        if added and not dry:
            doc.write("\n".join(out) + trailing_newline(doc.read()))
        report[doc.path.name] = {"добавлено": added, "уже стояло": skipped}
    for name, counts in report.items():
        if counts["добавлено"] or counts["уже стояло"]:
            print(
                f"anchors: {name}: +{counts['добавлено']}, уже стояло {counts['уже стояло']}"
            )


def trailing_newline(original: str) -> str:
    return "\n" if original.endswith("\n") else ""


def build_targets(docs: list[Doc]) -> dict[str, tuple[str, str]]:
    """ID -> (имя файла для wikilink, якорь ^id) по фактическим block-ID."""
    targets: dict[str, tuple[str, str]] = {}
    for doc in docs:  # Правила раньше Регламента — канонические цели приоритетны
        for bid in sorted(doc.block_ids):
            targets.setdefault(bid, (doc.stem, f"^{bid}"))
    return targets


def appendix_map(docs: list[Doc]) -> dict[str, Doc]:
    """Буква приложения -> документ (первый токен имени файла)."""
    return {d.stem.split(" ", 1)[0]: d for d in docs if d.role == "appendix"}


def wikilink(target: str, anchor: str | None, label: str) -> str:
    loc = f"{target}#{anchor}" if anchor else target
    return f"[[{loc}|{label}]]"


def suppress_range(text: str, s: int, e: int) -> bool:
    """НР-4: диапазоны и слэш-перечисления остаются плоским текстом."""
    if RANGE_AFTER.match(text[e:]):
        return True
    if text[e:].startswith("/"):
        return True
    if RANGE_BEFORE.search(text[:s].rstrip()[-2:] if s >= 2 else ""):
        return True
    head = text[:s]
    if head and head.rstrip() and head.rstrip()[-1] in "…–—/-":
        return True
    return False


def cmd_linkify(root: Path, dry: bool = False) -> None:
    """Плоские отсылки -> wikilinks: первое упоминание цели в разделе ##."""
    docs = kit_docs(root)
    for d in docs:
        d.scan()
    targets = build_targets(docs)
    appmap = appendix_map(docs)
    missing = sorted(
        f"{bid}"
        for bid, (stem, _) in targets.items()
        if stem not in {d.stem for d in docs}
    )
    if missing:
        sys.exit(f"linkify: нестандартные цели якорей: {missing}; выполните anchors")
    unresolved_points: list[str] = []
    total = 0
    for doc in docs:
        if doc.role == "rules":
            continue  # правила содержат примеры ссылок в коде — не конвертируем
        lines = doc.read().splitlines()
        out = []
        linked: set[str] = set()
        per_doc = 0
        fenced = False
        self_def: str | None = None
        for line in lines:
            m = REQ_DEF.match(line) or PR_DEF.match(line)
            self_def = m.group(1) if m else None
            tail = PARA_ANCHOR.search(line)  # якорь строки-определения
            if tail:
                self_def = tail.group(1)
            if FENCE.match(line):
                fenced = not fenced
                out.append(line)
                continue
            if fenced or HEADING.match(line) or TABLE_ROW.match(line):
                if H2.match(line):
                    linked.clear()
                out.append(line)
                continue
            if tail:
                out.append(line)
                continue  # НР-4/дословность: строка-определение не линкуется
            masked = mask_all(line)
            edits: list[tuple[int, int, str, str]] = []  # start, end, canonical, замена
            pending: set[str] = set()  # цели, намеченные на этой строке
            for m in ID_ANY.finditer(masked):
                bid = m.group(1)
                if bid == self_def:
                    continue
                s, e = m.start(), m.end()
                if s > 0 and masked[s - 1] == "^":
                    continue  # хвостовой block-ID якорь, не упоминание
                if not is_plain_boundary(masked, s, e):
                    continue
                if suppress_range(masked, s, e):
                    continue
                target = targets.get(bid)
                if not target:
                    continue
                stem, anchor = target
                canon = f"{stem}#{anchor}"
                if canon in linked or canon in pending:
                    continue
                pending.add(canon)
                edits.append((s, e, canon, wikilink(stem, anchor, bid)))
            for m in APPENDIX_REF.finditer(masked):
                s, e = m.start(), m.end()
                if s > 0 and masked[s - 1].isalnum():
                    continue
                letter, point = m.group(1), m.group(2)
                app = appmap.get(letter)
                if not app:
                    unresolved_points.append(
                        f"{doc.path.name}: «{m.group(0)}» — нет приложения {letter}"
                    )
                    continue
                if point:
                    prefix = f"{letter}.{point}."
                    heading = next(
                        (h for h in app.headings if h.startswith(prefix)), None
                    )
                    if not heading:
                        unresolved_points.append(
                            f"{doc.path.name}: «{m.group(0)}» — нет пункта {prefix}"
                        )
                        continue
                    canon = f"{app.stem}#{heading}"
                else:
                    canon = app.stem
                if canon in linked or canon in pending:
                    continue
                pending.add(canon)
                edits.append(
                    (
                        s,
                        e,
                        canon,
                        wikilink(app.stem, heading if point else None, m.group(0)),
                    )
                )
            if not edits:
                out.append(line)
                continue
            new_line = line
            for s, e, canon, repl in sorted(edits, key=lambda t: -t[0]):
                new_line = new_line[:s] + repl + new_line[e:]
                linked.add(canon)
                per_doc += 1
            out.append(new_line)
        if per_doc and not dry:
            doc.write("\n".join(out) + trailing_newline(doc.read()))
        if per_doc:
            print(f"linkify: {doc.path.name}: +{per_doc}")
        total += per_doc
    print(f"linkify: всего ссылок +{total}")
    for note in unresolved_points:
        print(f"linkify: НЕ разрешено (оставлено плоским): {note}")


def cmd_check(root: Path) -> list[str]:
    """Проверка НР-2: резолв всех wikilinks, уникальность и формат якорей."""
    docs = kit_docs(root)
    for d in docs:
        d.scan()
    by_stem = {d.stem: d for d in docs}
    errors: list[str] = []
    seen: dict[str, str] = {}
    links = 0
    for doc in docs:
        for bid in sorted(doc.block_ids):
            if not ANCHOR_FORMAT.match(bid):
                errors.append(f"{doc.path.name}: якорь ^{bid} вне формата НР-2")
            if bid in seen:
                errors.append(f"дубликат якоря ^{bid}: {seen[bid]} и {doc.path.name}")
            else:
                seen[bid] = doc.path.name
    for doc in docs:
        fenced = False
        for lineno, line in enumerate(doc.read().splitlines(), 1):
            if FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            for m in WIKILINK.finditer(mask_code(line)):
                inner = m.group(1)
                parts = inner.split("|", 1)
                loc = parts[0]
                if "#" in loc:
                    stem, anchor = loc.split("#", 1)
                else:
                    stem, anchor = loc, None
                target = by_stem.get(stem.strip())
                if not target:
                    errors.append(f"{doc.path.name}:{lineno}: файл «{stem}» не найден")
                    continue
                if anchor:
                    if anchor.startswith("^"):
                        if anchor[1:] not in target.block_ids:
                            errors.append(
                                f"{doc.path.name}:{lineno}: якорь {anchor} отсутствует в {stem}"
                            )
                    elif anchor.strip() not in target.headings:
                        errors.append(
                            f"{doc.path.name}:{lineno}: заголовок «{anchor}» отсутствует в {stem}"
                        )
                links += 1
    print(
        f"check: файлов {len(docs)}, wikilink-ссылок {links}, якорей {len(seen)}, ошибок {len(errors)}"
    )
    for e in errors:
        print(f"check: ОШИБКА: {e}")
    return errors


# ---------------------------------------------------------------- whitespace-guard

INLINE_STASH = re.compile(r"`[^`]*`")


def normalize_md(text: str) -> str:
    """Нормализация для сравнения: свёртка пробелов вне кода, канонизация
    разделителей таблиц. Авто-форматтер Obsidian меняет только выравнивание
    (пробелы/длину разделительной строки) — нормализованные версии совпадают.
    """
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            out.append(line.rstrip())
            continue
        if fenced:
            out.append(line)  # код-блок: значимые пробелы не трогаем
            continue
        if TABLE_ROW.match(line) and SEP_ROW.match(line):
            n = line.count("|")
            out.append("|" + "---|" * (n - 1))
            continue
        stash: list[str] = []

        def _put(m: re.Match) -> str:
            stash.append(m.group(0))
            return f"\x01{len(stash) - 1}\x01"

        s = INLINE_STASH.sub(_put, line.rstrip())
        s = re.sub(r"  +", " ", s)
        s = re.sub(r"\x01(\d+)\x01", lambda m: stash[int(m.group(1))], s)
        out.append(s)
    return "\n".join(out).strip("\n") + ("\n" if out else "")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )


def cmd_guard(root: Path, dry: bool = False) -> int:
    """Whitespace-guard: откат whitespace-артефактов форматирования таблиц.

    Авто-форматтер Obsidian выравнивает таблицы пробелами (содержание не меняет).
    По прецеденту п. 5 реестра источников такие правки откатываются из Git.
    Файл откатывается ТОЛЬКО если нормализованные версии HEAD и рабочей копии
    совпадают (изменения только пробельные); содержательные изменения остаются.
    Код выхода: 0 — артефактов нет/откачены; 1 — остались содержательные изменения.
    """
    docs = kit_docs(root)
    reverted: list[str] = []
    kept: list[str] = []
    missing_head: list[str] = []
    for doc in docs:
        rel = doc.path.relative_to(root).as_posix()
        show = _git(root, "show", f"HEAD:{rel}")
        if show.returncode != 0:
            if doc.path.exists():
                missing_head.append(rel)
            continue
        head = show.stdout
        try:
            work = doc.read()
        except FileNotFoundError:
            kept.append(rel + " (удалён)")
            continue
        if work == head:
            continue
        if normalize_md(work) == normalize_md(head):
            if not dry:
                _git(root, "checkout", "--", rel)
            reverted.append(rel)
        else:
            kept.append(rel)
    for rel in reverted:
        print(f"guard: откат whitespace-артефакта: {rel}")
    for rel in missing_head:
        print(f"guard: без версии в HEAD (новый): {rel}")
    for rel in kept:
        print(f"guard: СОДЕРЖАТЕЛЬНЫЕ ИЗМЕНЕНИЯ (оставлено): {rel}")
    print(f"guard: откатов {len(reverted)}, содержательных {len(kept)}")
    return 1 if kept else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("anchors", "linkify", "check", "guard"))
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--dry", action="store_true", help="не записывать изменения")
    args = parser.parse_args(argv)
    if args.command == "anchors":
        cmd_anchors(args.root, dry=args.dry)
        return 0
    if args.command == "linkify":
        cmd_linkify(args.root, dry=args.dry)
        return 0
    if args.command == "guard":
        return cmd_guard(args.root, dry=args.dry)
    return 1 if cmd_check(args.root) else 0


if __name__ == "__main__":
    sys.exit(main())
