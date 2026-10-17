#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Локальный помощник рецензирования (контур R).

Реализация шага А2 плана «План-инструкция по автоматизации рецензирования».
Только стандартная библиотека Python 3. Без LLM API, сети, базы данных,
Git-мутаций и произвольного shell. Помощник фиксирует входы, проверяет
формальные условия и собирает след решений; он не утверждает содержание.

Коды выхода:
  0 — запрошенная формальная операция выполнена (НЕ одобрение содержания);
  1 — выявлены нарушения проверяемого контракта;
  2 — операция не выполнена из-за входов/среды.

Запуск: uv run python tools/review.py --help
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

HELPER_VERSION = "0.1.1"
SCHEMA_VERSION = 1
DEFAULT_ROOT = Path(__file__).resolve().parent.parent
RUNS_SUBDIR = PurePosixPath("wrk/review-runs")

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_INPUT = 2

CATEGORIES = {
    "contradiction",
    "ambiguity",
    "incompleteness",
    "traceability",
    "formal_defect",
    "improvement",
}
SEVERITIES = {"blocker", "major", "minor", "suggestion"}
VERDICTS = {"needs_changes", "no_findings", "incomplete"}
COVERAGES = {"complete", "partial", "blocked"}
ACTIONS = {"accept", "reject", "defer"}
RESOLUTION_STATUSES = {"resolved", "not_resolved", "partially_resolved"}
APPROVAL_DECISIONS = {"approve", "revise", "stop"}
PERMITTED_ACTIONS = {"apply_changes"}
MODES = {"standard_review"}  # pilot_model_check / model_compliance — этап А4+
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
FINDING_ID_RE = re.compile(r"^REV-\d{3}$")

STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETED = "completed"
STATE_COMPLETED_NO_CHANGES = "completed_without_changes"
STATE_STOPPED = "stopped_with_open_items"


class ReviewError(Exception):
    """Нарушение проверяемого контракта → код 1."""


class InputError(Exception):
    """Ошибочные входы/среда → код 2."""


# ---------------------------------------------------------------- утилиты


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(Path(path).read_bytes())


def canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def compact_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_json_file(path: Path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise InputError(f"не удалось прочитать {path}: {e}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise InputError(f"некорректный JSON в {path}: {e}")


def safe_rel_path(root: Path, rel, must_exist: bool = True) -> Path:
    """Путь внутри корня: без абсолютных путей, .. и symlink; только файлы."""
    if not isinstance(rel, str) or not rel.strip() or rel != rel.strip():
        raise InputError(f"недопустимый путь: {rel!r}")
    p = PurePosixPath(rel)
    if p.is_absolute():
        raise InputError(f"абсолютный путь не допускается: {rel}")
    if any(part in ("..", "") for part in p.parts):
        raise InputError(f"выход за разрешённые пути не допускается: {rel}")
    root_resolved = root.resolve()
    cur = root
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            raise InputError(f"symlink в пути не допускается: {rel}")
    resolved = (root / p).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise InputError(f"путь вне корня проекта не допускается: {rel}")
    if must_exist:
        if not resolved.exists():
            raise InputError(f"файл не найден: {rel}")
        if not resolved.is_file():
            raise InputError(f"вход не является обычным файлом: {rel}")
    return resolved


def resolve_arg_path(root: Path, arg: str) -> Path:
    """Аргумент --task/--file: абсолютный внутри корня или относительный корня."""
    p = Path(arg)
    if p.is_absolute():
        resolved = p.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise InputError(f"путь вне корня проекта: {arg}")
        return resolved
    return safe_rel_path(root, arg)


def git_read(root: Path, args):
    try:
        r = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


# ------------------------------------------------------- проверка цитат


def _strip_blockquote(line: str) -> str:
    s = line.lstrip()
    if s.startswith(">"):
        s = s[1:]
        if s.startswith(" "):
            s = s[1:]
    return s.replace("`", "")


def _collapse_with_map(text: str):
    """Схлопнутый текст + карта «позиция → номер строки» (маркеры > убраны)."""
    out = []
    mapping = []
    for i, line in enumerate(text.split("\n")):
        s = _strip_blockquote(line)
        for j, word in enumerate(s.split()):
            if out:
                out.append(" ")
                mapping.append(i + 1)
            for ch in word:
                out.append(ch)
                mapping.append(i + 1)
    return "".join(out), mapping


def quote_matches(text: str, quote: str, start_line: int, end_line: int) -> bool:
    """Цитата найдена в файле, и заявленный диапазон покрывает её место
    или находится внутри него (якорная строка). Пробелы/переносы схлопываются."""
    q = quote.lstrip()
    if q.startswith(">"):
        q = q[1:]
        if q.startswith(" "):
            q = q[1:]
    q = q.replace("`", "")
    q = re.sub(r"\s+", " ", q).strip()
    if not q:
        return False
    collapsed, mapping = _collapse_with_map(text)
    if len(mapping) != len(collapsed):
        return False
    qlen = len(q)
    pos = collapsed.find(q)
    while pos >= 0:
        first = mapping[pos]
        last = mapping[pos + qlen - 1]
        if (first <= start_line and end_line <= last) or (
            start_line <= first and last <= end_line
        ):
            return True
        pos = collapsed.find(q, pos + 1)
    return False


# ------------------------------------------------------------ структура


def round_dir(run: Path, n: int) -> Path:
    return run / "rounds" / f"{n:02d}"


def append_event(run: Path, actor: str, event: str, round_no, details: dict) -> None:
    path = run / "events.jsonl"
    seq = 0
    if path.exists():
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if lines:
            try:
                seq = json.loads(lines[-1]).get("seq", 0)
            except json.JSONDecodeError:
                seq = len(lines)
    record = {
        "seq": seq + 1,
        "datetime_utc": utc_now(),
        "actor": actor,
        "event": event,
        "round": round_no,
        "details": details,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(compact_json(record) + "\n")


def load_task_of_run(run: Path) -> dict:
    task = load_json_file(run / "task.json")
    if not isinstance(task, dict):
        raise ReviewError("task.json запуска повреждён")
    return task


def load_manifest(run: Path) -> dict:
    m = load_json_file(run / "manifest.json")
    if not isinstance(m, dict) or m.get("schema_version") != SCHEMA_VERSION:
        raise ReviewError("manifest.json запуска повреждён")
    return m


def base_files_of(manifest: dict, base_root: Path) -> dict:
    """Путь → текст файла базы (снимок или кандидат предыдущего раунда)."""
    result = {}
    for f in manifest["files"]:
        p = base_root / f["path"]
        result[f["path"]] = p.read_text(encoding="utf-8")
    return result


def collect_finding_ids(run: Path) -> set:
    ids = set()
    if not (run / "rounds").exists():
        return ids
    for rd in sorted((run / "rounds").iterdir()):
        for name in ("review.json", "verification.json"):
            p = rd / name
            if p.is_file():
                doc = load_json_file(p)
                for f in doc.get("findings", []):
                    ids.add(f["id"])
    return ids


def load_round_decisions(run: Path, n: int) -> dict:
    p = round_dir(run, n) / "decisions.json"
    if not p.is_file():
        raise ReviewError(f"решения по замечаниям раунда {n} не записаны")
    return load_json_file(p)


def accepted_findings(decisions: dict) -> list:
    return [d["finding_id"] for d in decisions["decisions"] if d["action"] == "accept"]


def load_round_approval(run: Path, n: int):
    p = round_dir(run, n) / "approval.json"
    if not p.is_file():
        return None
    return load_json_file(p)


def verify_snapshot(run: Path, manifest: dict) -> None:
    """Неизменность базы: манифест, снимок, копия задания, чек-лист."""
    events_path = run / "events.jsonl"
    created = None
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        ev = json.loads(line)
        if ev.get("event") == "run_created":
            created = ev
    if created is None or created["details"].get("manifest_sha256") != sha_file(
        run / "manifest.json"
    ):
        raise ReviewError(
            "манифест изменён или заменён после создания запуска; "
            "хеши не пересчитываются для сокрытия изменения"
        )
    mismatches = []
    for f in manifest["files"]:
        p = run / "snapshot" / f["path"]
        if not p.is_file():
            mismatches.append(f"{f['path']} (отсутствует в снимке)")
        elif sha_file(p) != f["sha256"]:
            mismatches.append(f["path"])
    if mismatches:
        raise ReviewError("база изменена после снимка: " + ", ".join(mismatches))
    if sha_file(run / "task.json") != manifest["task"]["sha256"]:
        raise ReviewError("копия задания (task.json) не совпадает с манифестом")
    cl = run.parent.parent.parent / manifest["checklist"]["path"]
    if not cl.is_file() or sha_file(cl) != manifest["checklist"]["sha256"]:
        raise ReviewError(
            f"чек-лист {manifest['checklist']['path']} изменён после создания запуска"
        )


def load_checklist(root: Path, manifest: dict) -> dict:
    path = root / manifest["checklist"]["path"]
    doc = load_json_file(path)
    if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
        raise ReviewError("чек-лист повреждён")
    ids = [i["id"] for i in doc.get("items", []) if isinstance(i, dict) and "id" in i]
    if not ids:
        raise ReviewError("чек-лист не содержит пунктов")
    return {"doc": doc, "ids": ids, "path": path}


def current_base(run: Path, manifest: dict, n: int, phase: str):
    """База прохода: (путь к манифесту базы, хеш, корень файлов, хеши файлов)."""
    if phase == "verification":
        cm_path = round_dir(run, n) / "candidate-manifest.json"
        if not cm_path.is_file():
            raise ReviewError(f"кандидат раунда {n} не зафиксирован (нет манифеста)")
        cm = load_json_file(cm_path)
        cand_root = round_dir(run, n) / "candidate"
        for f in cm["files"]:
            p = cand_root / f["path"]
            if not p.is_file() or sha_file(p) != f["sha256"]:
                raise ReviewError(
                    f"кандидат раунда {n} не совпадает с манифестом: {f['path']}"
                )
        return cm_path, sha_file(cm_path), cand_root, cm
    if n == 1:
        files = {f["path"]: f["sha256"] for f in manifest["files"]}
        return (
            run / "manifest.json",
            sha_file(run / "manifest.json"),
            run / "snapshot",
            {"files": manifest["files"]},
        )
    prev_approval = load_round_approval(run, n - 1)
    if prev_approval is None or prev_approval.get("decision") != "revise":
        raise ReviewError(f"раунд {n} требует решения revise по раунду {n - 1}")
    cm_path = round_dir(run, n - 1) / "candidate-manifest.json"
    if not cm_path.is_file():
        raise ReviewError(f"кандидат раунда {n - 1} не найден")
    cm = load_json_file(cm_path)
    cand_root = round_dir(run, n - 1) / "candidate"
    for f in cm["files"]:
        p = cand_root / f["path"]
        if not p.is_file() or sha_file(p) != f["sha256"]:
            raise ReviewError(
                f"кандидат раунда {n - 1} не совпадает с манифестом: {f['path']}"
            )
    return cm_path, sha_file(cm_path), cand_root, cm


# ------------------------------------------------------------ prepare


def validate_task(root: Path, task: dict) -> None:
    if not isinstance(task, dict):
        raise InputError("задание должно быть JSON-объектом")
    if task.get("schema_version") != SCHEMA_VERSION:
        raise InputError(
            f"schema_version задания должен быть {SCHEMA_VERSION}, получено: "
            f"{task.get('schema_version')!r}"
        )
    for field in ("task_id", "purpose", "scope"):
        v = task.get(field)
        if not isinstance(v, str) or not v.strip():
            raise InputError(f"обязательное поле задания отсутствует: {field}")
    mode = task.get("mode")
    if mode not in MODES:
        raise InputError(
            f"mode {mode!r} не поддерживается в первой версии; допустимо: "
            + ", ".join(sorted(MODES))
            + " (контуры P/C — этап А4)"
        )
    inputs = task.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise InputError("inputs: непустой список относительных путей")
    if len(set(inputs)) != len(inputs):
        raise InputError("inputs: дубликаты путей")
    for rel in inputs:
        safe_rel_path(root, rel, must_exist=True)
    allowed = task.get("allowed_candidate_paths", [])
    if not isinstance(allowed, list):
        raise InputError("allowed_candidate_paths должен быть списком")
    for rel in allowed:
        if rel not in inputs:
            raise InputError(f"allowed_candidate_paths содержит путь вне inputs: {rel}")
    cl = task.get("checklist_path")
    if not isinstance(cl, str):
        raise InputError("checklist_path обязателен")
    safe_rel_path(root, cl, must_exist=True)
    mr = task.get("max_rounds")
    if not isinstance(mr, int) or isinstance(mr, bool) or not (1 <= mr <= 5):
        raise InputError("max_rounds: целое от 1 до 5")
    roles = task.get("roles")
    if not isinstance(roles, dict):
        raise InputError("roles: объект с ролями")
    for role in ("reviewer", "editor", "decision_maker", "assistant"):
        v = roles.get(role)
        if not isinstance(v, str) or not v.strip():
            raise InputError(f"roles.{role}: непустая строка")
    env = task.get("environment")
    if not isinstance(env, dict):
        raise InputError("environment: объект с описанием среды LLM")
    for key in ("llm_profile", "provider_model"):
        if not isinstance(env.get(key), str):
            raise InputError(
                f"environment.{key}: обязательная строка (может быть «не указана»)"
            )


def cmd_prepare(root: Path, args) -> int:
    task_path = resolve_arg_path(root, args.task)
    task = load_json_file(task_path)
    validate_task(root, task)
    if not RUN_ID_RE.match(args.run):
        raise InputError(
            f"run-id допускает буквы/цифры/._- (до 64 символов): {args.run!r}"
        )
    run = root / RUNS_SUBDIR / args.run
    if run.exists():
        raise InputError(f"каталог запуска уже существует — не перезаписывается: {run}")
    run.mkdir(parents=True)

    files = []
    for rel in sorted(task["inputs"]):
        src = root / rel
        data = src.read_bytes()
        dst = run / "snapshot" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(dst, data)
        files.append({"path": rel, "sha256": sha_bytes(data), "size": len(data)})
    task_data = canonical_json(task).encode("utf-8")
    atomic_write(run / "task.json", task_data)

    checklist = load_json_file(root / task["checklist_path"])
    commit = git_read(root, ["rev-parse", "--verify", "HEAD"])
    status = git_read(root, ["status", "--porcelain"])
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run,
        "created_at": utc_now(),
        "helper_version": HELPER_VERSION,
        "base_commit": commit.strip() if commit else None,
        "base_commit_dirty": bool(status.strip()) if status is not None else None,
        "task": {
            "task_id": task["task_id"],
            "path": str(task_path.resolve().relative_to(root.resolve())),
            "sha256": sha_bytes(task_data),
            "size": len(task_data),
        },
        "checklist": {
            "path": task["checklist_path"],
            "sha256": sha_file(root / task["checklist_path"]),
            "version": checklist.get("version", ""),
        },
        "files": files,
    }
    atomic_write(run / "manifest.json", canonical_json(manifest).encode("utf-8"))
    append_event(
        run,
        "helper",
        "run_created",
        1,
        {
            "task_id": task["task_id"],
            "manifest_sha256": sha_file(run / "manifest.json"),
            "base_commit": manifest["base_commit"],
            "base_commit_dirty": manifest["base_commit_dirty"],
        },
    )
    print(f"Запуск {args.run} создан: {run}")
    print(
        f"Манифест: sha256={manifest['task']['sha256'][:12]}… (задание), файлов: {len(files)}"
    )
    return EXIT_OK


# ------------------------------------------------------------ check


def cmd_check(root: Path, args) -> int:
    run = root / RUNS_SUBDIR / args.run
    if not run.is_dir():
        raise InputError(f"запуск не найден: {args.run}")
    manifest = load_manifest(run)
    if args.target == "snapshot":
        try:
            verify_snapshot(run, manifest)
        except ReviewError as e:
            append_event(run, "helper", "integrity_violation", None, {"reason": str(e)})
            raise
        drift = []
        for f in manifest["files"]:
            src = root / f["path"]
            if src.is_file() and sha_file(src) != f["sha256"]:
                drift.append(f["path"])
        if drift:
            print(
                "ПРЕДУПРЕЖДЕНИЕ (информационно): источники изменились после снимка: "
                + ", ".join(drift)
                + "; базой проверки остаётся снимок"
            )
        append_event(
            run,
            "helper",
            "snapshot_checked",
            None,
            {"source_drift": drift},
        )
        print("Снимок неизменен; формальные условия проверки базы выполнены.")
        return EXIT_OK

    # target == candidate
    n = args.round
    task = load_task_of_run(run)
    if not (1 <= n <= task["max_rounds"]):
        raise ReviewError(
            f"раунд {n} вне допустимого диапазона 1..{task['max_rounds']}"
        )
    verify_snapshot(run, manifest)
    decisions = load_round_decisions(run, n)
    accepted = accepted_findings(decisions)
    if not accepted:
        raise InputError(
            "принятых замечаний нет (no_fixes_confirmed) — кандидат не требуется; "
            "переходите к record-approval"
        )
    cand_root = round_dir(run, n) / "candidate"
    if not cand_root.is_dir():
        raise InputError(
            f"каталог кандидата не найден: {round_dir(run, n) / 'candidate'}"
        )
    allowed = set(task["allowed_candidate_paths"])
    input_set = set(task["inputs"])

    present = set()
    for p in cand_root.rglob("*"):
        if p.is_file():
            present.add(p.relative_to(cand_root).as_posix())
    missing = sorted(input_set - present)
    extra = sorted(present - input_set)
    problems = []
    if missing:
        problems.append("в кандидате отсутствуют файлы: " + ", ".join(missing))
    if extra:
        problems.append("в кандидате посторонние файлы: " + ", ".join(extra))
    if problems:
        raise ReviewError("; ".join(problems))

    changed = []
    for rel in sorted(input_set):
        cand = cand_root / rel
        base = run / "snapshot" / rel
        if not allowed.__contains__(rel) and cand.read_bytes() != base.read_bytes():
            raise ReviewError(f"файл вне allowed_candidate_paths изменён: {rel}")
        if cand.read_bytes() != base.read_bytes():
            changed.append(rel)

    if n > 1:
        prev_manifest = load_json_file(
            round_dir(run, n - 1) / "candidate-manifest.json"
        )
        # файлы, уже числившиеся изменёнными по итогам раунда n-1:
        # отличаются от исходного снимка или от кандидата раунда n-2
        # (того же состава, что список changed, который helper вычислял
        # для раунда n-1)
        snap_sha = {f["path"]: f["sha256"] for f in manifest["files"]}
        prev_sha = {f["path"]: f["sha256"] for f in prev_manifest["files"]}
        before_sha = snap_sha
        if n > 2:
            before_prev = load_json_file(
                round_dir(run, n - 2) / "candidate-manifest.json"
            )
            before_sha = {f["path"]: f["sha256"] for f in before_prev["files"]}
        already_changed = {
            p
            for p in prev_sha
            if prev_sha[p] != snap_sha.get(p) or prev_sha[p] != before_sha.get(p)
        }
        # принятые замечания предыдущих раундов: допустимы как атрибуция
        # для файлов, изменённых до текущего раунда (раунд n может править
        # подмножество файлов раунда n-1 — остальные обязаны числиться
        # в changes.json с ID своих раундов)
        prev_accepted = set()
        for k in range(1, n):
            prev_accepted.update(accepted_findings(load_round_decisions(run, k)))
        prev_root = round_dir(run, n - 1) / "candidate"
        for f in prev_manifest["files"]:
            if (cand_root / f["path"]).read_bytes() != (
                prev_root / f["path"]
            ).read_bytes():
                if f["path"] not in changed:
                    changed.append(f["path"])
    else:
        already_changed = set()
        prev_accepted = set()

    changes_path = round_dir(run, n) / "changes.json"
    if changed:
        if not changes_path.is_file():
            raise ReviewError(
                "изменённые файлы не описаны в changes.json: " + ", ".join(changed)
            )
        changes = load_json_file(changes_path)
        if changes.get("schema_version") != SCHEMA_VERSION:
            raise ReviewError("changes.json: schema_version должен быть 1")
        if changes.get("run_id") != args.run or changes.get("round") != n:
            raise ReviewError("changes.json: несоответствие run_id/round")
        base_manifest_hash = (
            sha_file(run / "manifest.json")
            if n == 1
            else sha_file(round_dir(run, n - 1) / "candidate-manifest.json")
        )
        if changes.get("base_manifest_hash") != base_manifest_hash:
            raise ReviewError(
                "changes.json: base_manifest_hash не совпадает с базой раунда"
            )
        listed = {}
        for c in changes.get("changes", []):
            path = c.get("path")
            if path not in input_set:
                raise ReviewError(f"changes.json: путь вне комплекта: {path}")
            if path in listed:
                raise ReviewError(f"changes.json: путь указан дважды: {path}")
            ids = c.get("finding_ids")
            if not isinstance(ids, list) or not ids:
                raise ReviewError(f"changes.json: пустые finding_ids для {path}")
            for fid in ids:
                if fid in accepted:
                    continue
                if path in already_changed and fid in prev_accepted:
                    continue
                if path in already_changed:
                    raise ReviewError(
                        f"changes.json: ID {fid} не входит в принятые замечания "
                        f"раунда {n} и в принятые замечания предыдущих раундов "
                        f"(файл {path} изменён до текущего раунда)"
                    )
                raise ReviewError(
                    f"changes.json: ID {fid} не входит в принятые замечания раунда {n}"
                )
            listed[path] = ids
        unlisted = [p for p in changed if p not in listed]
        if unlisted:
            raise ReviewError(
                "изменённые файлы не перечислены в changes.json: " + ", ".join(unlisted)
            )
        stale = [p for p in listed if p not in changed]
        if stale:
            raise ReviewError(
                "changes.json описывает неизменённые файлы: " + ", ".join(stale)
            )

    files = []
    for rel in sorted(input_set):
        data = (cand_root / rel).read_bytes()
        files.append({"path": rel, "sha256": sha_bytes(data), "size": len(data)})
    candidate_manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run,
        "round": n,
        "created_at": utc_now(),
        "helper_version": HELPER_VERSION,
        "base_manifest_hash": sha_file(run / "manifest.json"),
        "files": files,
    }
    cm_path = round_dir(run, n) / "candidate-manifest.json"
    idempotent = False
    if cm_path.is_file():
        existing = load_json_file(cm_path)
        same_files = existing.get("files") == candidate_manifest["files"]
        if same_files:
            idempotent = True
        else:
            if (round_dir(run, n) / "verification.json").is_file():
                raise ReviewError(
                    "кандидат изменён после повторного ревью; требуется новый "
                    "кандидат и повторная проверка"
                )
    if not idempotent:
        atomic_write(cm_path, canonical_json(candidate_manifest).encode("utf-8"))
    append_event(
        run,
        "helper",
        "candidate_checked",
        n,
        {
            "candidate_manifest_sha256": sha_file(cm_path),
            "files_changed": changed,
            "idempotent": idempotent,
        },
    )
    print(
        f"Кандидат раунда {n} проверен: состав полный, allowlist соблюдён, "
        f"изменено файлов: {len(changed)}"
        + (" (повторная запись без изменений)" if idempotent else "")
    )
    return EXIT_OK


# ------------------------------------------------------ record-review


def _require_str(obj, field, ctx) -> str:
    v = obj.get(field)
    if not isinstance(v, str) or not v.strip():
        raise ReviewError(f"{ctx}: поле {field} обязательно (непустая строка)")
    return v


def validate_finding(f, ctx, base_texts, base_hashes, checklist_ids, seen_ids):
    fid = f.get("id")
    if not isinstance(fid, str) or not FINDING_ID_RE.match(fid):
        raise ReviewError(f"{ctx}: некорректный ID замечания: {fid!r} (формат REV-NNN)")
    if fid in seen_ids:
        raise ReviewError(f"{ctx}: дубликат ID замечания в рамках запуска: {fid}")
    seen_ids.add(fid)
    if f.get("category") not in CATEGORIES:
        raise ReviewError(
            f"{ctx} [{fid}]: недопустимая категория: {f.get('category')!r}"
        )
    if f.get("severity") not in SEVERITIES:
        raise ReviewError(
            f"{ctx} [{fid}]: недопустимая строгость: {f.get('severity')!r}"
        )
    _require_str(f, "title", f"{ctx} [{fid}]")

    def check_location(loc, quote, label):
        if not isinstance(loc, dict):
            raise ReviewError(f"{ctx} [{fid}]: {label} обязателен")
        path = loc.get("path")
        if path not in base_hashes:
            raise ReviewError(
                f"{ctx} [{fid}]: {label}.path не входит в базу прохода: {path}"
            )
        # sha256 опционален: версия файла определяется base_manifest_hash отчёта;
        # если рецензент указал sha256 — он обязан совпадать с базой
        declared_sha = loc.get("sha256")
        if declared_sha is not None and declared_sha != base_hashes[path]:
            raise ReviewError(
                f"{ctx} [{fid}]: sha256 файла {path} не совпадает с базой "
                f"({label}): возможно, цитата из другой версии; надёжнее "
                "опустить поле sha256"
            )
        text = base_texts[path]
        s, e = loc.get("start_line"), loc.get("end_line")
        if (
            not isinstance(s, int)
            or not isinstance(e, int)
            or not (1 <= s <= e <= len(text.splitlines()))
        ):
            raise ReviewError(
                f"{ctx} [{fid}]: некорректный диапазон строк {s}..{e} для {path} "
                f"(в файле {len(text.splitlines())} строк)"
            )
        if not isinstance(quote, str) or not quote.strip():
            raise ReviewError(f"{ctx} [{fid}]: {label}: цитата обязательна")
        if not quote_matches(text, quote, s, e):
            raise ReviewError(
                f"{ctx} [{fid}]: цитата не найдена в указанном месте ({path}, "
                f"строки {s}–{e}); отчёт не принимается как доказательный"
            )

    check_location(f.get("location"), f.get("quote"), "location")
    if f.get("category") == "contradiction":
        if "location2" not in f or "quote2" not in f:
            raise ReviewError(
                f"{ctx} [{fid}]: для противоречия обязательны location2 и quote2"
            )
        check_location(f.get("location2"), f.get("quote2"), "location2")
    basis = _require_str(f, "basis", f"{ctx} [{fid}]")
    if basis not in checklist_ids and not basis.lower().startswith(
        "методическая рекомендация"
    ):
        raise ReviewError(
            f"{ctx} [{fid}]: basis должен быть пунктом чек-листа "
            f"({', '.join(checklist_ids)}) или «методическая рекомендация»"
        )
    _require_str(f, "scenario", f"{ctx} [{fid}]")
    _require_str(f, "proposal", f"{ctx} [{fid}]")
    if f.get("status") != "new":
        raise ReviewError(f"{ctx} [{fid}]: status первичного замечания должен быть new")


def cmd_record_review(root: Path, args) -> int:
    run = root / RUNS_SUBDIR / args.run
    if not run.is_dir():
        raise InputError(f"запуск не найден: {args.run}")
    manifest = load_manifest(run)
    task = load_task_of_run(run)
    n = args.round
    phase = args.phase
    if not (1 <= n <= task["max_rounds"]):
        raise ReviewError(
            f"раунд {n} превышает max_rounds={task['max_rounds']} — "
            f"остановка: {STATE_STOPPED}"
        )
    if phase == "verification":
        _ = load_round_decisions(run, n)
    try:
        verify_snapshot(run, manifest)
    except ReviewError as e:
        append_event(run, "helper", "integrity_violation", n, {"reason": str(e)})
        raise
    doc = load_json_file(resolve_arg_path(root, args.file))
    ctx = f"отчёт ({phase}, раунд {n})"

    # идемпотентность/конфликт проверяются до содержательной валидации
    target = round_dir(run, n) / (
        "verification.json" if phase == "verification" else "review.json"
    )
    if target.is_file():
        existing = load_json_file(target)
        if canonical_json(existing) == canonical_json(doc):
            append_event(
                run, "helper", "idempotent_repeat", n, {"artifact": target.name}
            )
            print(f"Повторная запись идентичного содержимого: {target.name}")
            return EXIT_OK
        append_event(
            run,
            "helper",
            "validation_failed",
            n,
            {"reason": f"конфликт: {target.name} уже записан с другим содержимым"},
        )
        raise ReviewError(
            f"конфликт: {target.name} уже записан с другим содержимым; "
            "исправляйте артефакты новыми раундами, а не перезаписью"
        )

    base_manifest_path, base_hash, base_root, base_manifest = current_base(
        run, manifest, n, phase
    )
    base_hashes = {f["path"]: f["sha256"] for f in base_manifest["files"]}
    base_texts = {}
    for rel in base_hashes:
        base_texts[rel] = (base_root / rel).read_text(encoding="utf-8")
    checklist = load_checklist(root, manifest)

    problems = []
    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"{ctx}: schema_version должен быть {SCHEMA_VERSION}")
    if doc.get("run_id") != args.run:
        problems.append(f"{ctx}: run_id не совпадает")
    if doc.get("round") != n:
        problems.append(f"{ctx}: round не совпадает")
    if doc.get("phase") != phase:
        problems.append(f"{ctx}: phase не совпадает")
    if doc.get("base_manifest_hash") != base_hash:
        problems.append(f"{ctx}: base_manifest_hash не совпадает с базой прохода")
    criteria = doc.get("criteria") or {}
    if criteria.get("checklist_version") != manifest["checklist"]["version"]:
        problems.append(f"{ctx}: версия чек-листа не совпадает с манифестом")
    if criteria.get("checklist_sha256") != manifest["checklist"]["sha256"]:
        problems.append(f"{ctx}: sha256 чек-листа не совпадает с манифестом")
    reviewed = doc.get("reviewed_paths")
    if not isinstance(reviewed, list):
        problems.append(f"{ctx}: reviewed_paths должен быть списком")
    else:
        for p in reviewed:
            if p not in base_hashes:
                problems.append(f"{ctx}: reviewed_paths вне базы: {p}")
    unreviewed = doc.get("unreviewed", [])
    if not isinstance(unreviewed, list):
        problems.append(f"{ctx}: unreviewed должен быть списком")
    else:
        for u in unreviewed:
            # информационное поле: допустимы указатели на внешние материалы,
            # не входящие в пакет (справочники, реестры, внешние документы)
            if (
                not isinstance(u, dict)
                or not isinstance(u.get("path"), str)
                or not u["path"].strip()
                or not isinstance(u.get("reason"), str)
                or not u["reason"].strip()
            ):
                problems.append(f"{ctx}: некорректная запись unreviewed: {u!r}")
    coverage = doc.get("checklist_coverage")
    covered = {}
    if not isinstance(coverage, list):
        problems.append(f"{ctx}: checklist_coverage обязателен")
    else:
        for c in coverage:
            if not isinstance(c, dict) or c.get("item_id") not in checklist["ids"]:
                problems.append(f"{ctx}: неизвестный пункт чек-листа: {c!r}")
                continue
            if c["item_id"] in covered:
                problems.append(f"{ctx}: пункт чек-листа указан дважды: {c['item_id']}")
            status = c.get("status")
            if status not in ("checked", "not_checked"):
                problems.append(
                    f"{ctx}: недопустимый статус покрытия {status!r} для {c['item_id']}"
                )
            if status == "not_checked" and not (
                isinstance(c.get("reason"), str) and c["reason"].strip()
            ):
                problems.append(f"{ctx}: not_checked требует причину: {c['item_id']}")
            covered[c["item_id"]] = status
        absent = [i for i in checklist["ids"] if i not in covered]
        if absent:
            problems.append(
                f"{ctx}: покрытие чек-листа неполное, отсутствуют: " + ", ".join(absent)
            )
    if problems:
        append_event(run, "helper", "validation_failed", n, {"reasons": problems})
        raise ReviewError("; ".join(problems))

    seen_ids = collect_finding_ids(run)
    findings = doc.get("findings")
    if not isinstance(findings, list):
        raise ReviewError(f"{ctx}: findings должен быть списком")
    for f in findings:
        validate_finding(f, ctx, base_texts, base_hashes, checklist["ids"], seen_ids)

    if phase == "verification":
        round_decisions = load_round_decisions(run, n)
        accepted = accepted_findings(round_decisions)
        deferred = [
            d["finding_id"]
            for d in round_decisions["decisions"]
            if d["action"] == "defer"
        ]
        rc = doc.get("resolution_checks")
        if not isinstance(rc, list):
            raise ReviewError(
                f"{ctx}: resolution_checks обязателен в фазе verification"
            )
        got = [r.get("finding_id") for r in rc]
        missing = [fid for fid in accepted if fid not in got]
        if missing:
            raise ReviewError(
                f"{ctx}: resolution_checks должен покрывать все принятые замечания; "
                f"отсутствуют: {', '.join(missing)}"
            )
        for r in rc:
            if r.get("status") not in RESOLUTION_STATUSES:
                raise ReviewError(
                    f"{ctx}: недопустимый статус решения: {r.get('status')!r}"
                )
            if not (isinstance(r.get("note"), str) and r["note"].strip()):
                raise ReviewError(f"{ctx}: note обязателен в resolution_checks")
            fid = r.get("finding_id")
            if fid not in accepted and fid not in deferred:
                raise ReviewError(
                    f"{ctx}: resolution_checks ссылается на замечание вне решений "
                    f"раунда: {fid}"
                )
            if fid in deferred and r.get("status") != "not_resolved":
                raise ReviewError(
                    f"{ctx}: отложенное замечание {fid} может фигурировать только "
                    "со статусом not_resolved"
                )
    summary = doc.get("summary") or {}
    if summary.get("verdict") not in VERDICTS:
        raise ReviewError(f"{ctx}: недопустимый verdict: {summary.get('verdict')!r}")
    if summary.get("coverage") not in COVERAGES:
        raise ReviewError(f"{ctx}: недопустимый coverage: {summary.get('coverage')!r}")

    atomic_write(target, canonical_json(doc).encode("utf-8"))
    event = "verification_recorded" if phase == "verification" else "review_recorded"
    append_event(
        run,
        "helper",
        event,
        n,
        {
            "sha256": sha_file(target),
            "verdict": summary["verdict"],
            "coverage": summary["coverage"],
            "findings": len(findings),
            "base_manifest_hash": base_hash,
        },
    )
    print(f"Записан {target.name} (sha256={sha_file(target)[:12]}…)")
    print("Код 0 означает успешную формальную запись, а не одобрение содержания.")
    return EXIT_OK


# ------------------------------------------------------ record-decision


def cmd_record_decision(root: Path, args) -> int:
    run = root / RUNS_SUBDIR / args.run
    if not run.is_dir():
        raise InputError(f"запуск не найден: {args.run}")
    manifest = load_manifest(run)
    task = load_task_of_run(run)
    n = args.round
    if not (1 <= n <= task["max_rounds"]):
        raise ReviewError(f"раунд {n} вне диапазона 1..{task['max_rounds']}")
    review_path = round_dir(run, n) / "review.json"
    if not review_path.is_file():
        raise InputError(f"отчёт раунда {n} не записан; решения принимаются после него")
    verify_snapshot(run, manifest)
    if n == 1:
        base_hash = sha_file(run / "manifest.json")
    else:
        prev = load_round_approval(run, n - 1)
        if prev is None or prev.get("decision") != "revise":
            raise ReviewError(f"раунд {n} требует решения revise по раунду {n - 1}")
        base_hash = sha_file(round_dir(run, n - 1) / "candidate-manifest.json")

    doc = load_json_file(resolve_arg_path(root, args.file))

    # идемпотентность/конфликт до содержательной валидации
    target = round_dir(run, n) / "decisions.json"
    if target.is_file():
        existing = load_json_file(target)
        if canonical_json(existing) == canonical_json(doc):
            append_event(
                run, "helper", "idempotent_repeat", n, {"artifact": "decisions.json"}
            )
            print("Повторная запись идентичного содержимого: decisions.json")
            return EXIT_OK
        append_event(
            run,
            "helper",
            "validation_failed",
            n,
            {"reason": "конфликт: decisions.json уже записан с другим содержимым"},
        )
        raise ReviewError("конфликт: decisions.json уже записан с другим содержимым")

    problems = []
    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append("schema_version должен быть 1")
    if doc.get("run_id") != args.run or doc.get("round") != n:
        problems.append("run_id/round не совпадают")
    if doc.get("base_manifest_hash") != base_hash:
        problems.append("base_manifest_hash не совпадает с базой раунда")
    if doc.get("review_hash") != sha_file(review_path):
        problems.append("review_hash не совпадает с записанным отчётом")
    for field in ("decision_maker", "confirmation_basis"):
        if not (isinstance(doc.get(field), str) and doc[field].strip()):
            problems.append(f"поле {field} обязательно")
    review = load_json_file(review_path)
    review_ids = [f["id"] for f in review.get("findings", [])]
    decisions = doc.get("decisions")
    seen = set()
    if not isinstance(decisions, list):
        problems.append("decisions должен быть списком")
    else:
        for d in decisions:
            fid = d.get("finding_id")
            if fid not in review_ids:
                problems.append(f"неизвестное замечание: {fid}")
            if fid in seen:
                problems.append(f"повторное решение для {fid}")
            seen.add(fid)
            if d.get("action") not in ACTIONS:
                problems.append(f"недопустимое действие {d.get('action')!r} для {fid}")
            if not (isinstance(d.get("reason"), str) and d["reason"].strip()):
                problems.append(f"причина обязательна для {fid}")
        absent = [i for i in review_ids if i not in seen]
        if absent:
            problems.append(
                "нет решения по замечаниям (нет ответа — нет разрешения): "
                + ", ".join(absent)
            )
    no_fixes = doc.get("no_fixes_confirmed", False)
    if not isinstance(no_fixes, bool):
        problems.append("no_fixes_confirmed должен быть boolean")
    if problems:
        append_event(run, "helper", "validation_failed", n, {"reasons": problems})
        raise ReviewError("; ".join(problems))
    accepted = [d["finding_id"] for d in decisions if d["action"] == "accept"]
    if not accepted and not no_fixes:
        raise ReviewError(
            "принятых замечаний нет — подтвердите явно отсутствие правок "
            "(no_fixes_confirmed=true)"
        )
    if accepted and no_fixes:
        raise ReviewError(
            "no_fixes_confirmed=true противоречит принятым к исправлению замечаниям"
        )

    atomic_write(target, canonical_json(doc).encode("utf-8"))
    append_event(
        run,
        "helper",
        "decisions_recorded",
        n,
        {
            "sha256": sha_file(target),
            "accepted": accepted,
            "rejected": [d["finding_id"] for d in decisions if d["action"] == "reject"],
            "deferred": [d["finding_id"] for d in decisions if d["action"] == "defer"],
        },
    )
    print(
        f"Решения раунда {n} записаны: принято {len(accepted)}, "
        "нет ответа — нет разрешения на редактуру"
    )
    return EXIT_OK


# ---------------------------------------------------------------- diff


def cmd_diff(root: Path, args) -> int:
    run = root / RUNS_SUBDIR / args.run
    if not run.is_dir():
        raise InputError(f"запуск не найден: {args.run}")
    manifest = load_manifest(run)
    task = load_task_of_run(run)
    n = args.round
    if not (1 <= n <= task["max_rounds"]):
        raise ReviewError(f"раунд {n} вне диапазона 1..{task['max_rounds']}")
    verify_snapshot(run, manifest)
    cm_path = round_dir(run, n) / "candidate-manifest.json"
    if not cm_path.is_file():
        raise InputError(
            f"кандидат раунда {n} не зафиксирован; сначала check --target candidate"
        )
    cm = load_json_file(cm_path)
    cand_root = round_dir(run, n) / "candidate"
    for f in cm["files"]:
        p = cand_root / f["path"]
        if not p.is_file() or sha_file(p) != f["sha256"]:
            raise ReviewError(f"кандидат не совпадает с манифестом: {f['path']}")

    patches = []
    files_changed = []
    for f in cm["files"]:
        rel = f["path"]
        a = (
            (run / "snapshot" / rel)
            .read_text(encoding="utf-8")
            .splitlines(keepends=True)
        )
        b = (cand_root / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        if a != b:
            files_changed.append(rel)
            patches.extend(
                difflib.unified_diff(a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}")
            )
    patch_text = "".join(patches)
    diff_json = {
        "schema_version": SCHEMA_VERSION,
        "run_id": args.run,
        "round": n,
        "base_manifest_hash": sha_file(run / "manifest.json"),
        "candidate_manifest_hash": sha_file(cm_path),
        "diff_sha256": sha_bytes(patch_text.encode("utf-8")),
        "files_changed": files_changed,
    }
    dj_path = round_dir(run, n) / "diff.json"
    dp_path = round_dir(run, n) / "diff.patch"
    if dj_path.is_file():
        existing = load_json_file(dj_path)
        if existing.get("diff_sha256") == diff_json["diff_sha256"]:
            append_event(run, "helper", "idempotent_repeat", n, {"artifact": "diff"})
            print("Повторное построение идентичного diff.")
            return EXIT_OK
        if (round_dir(run, n) / "approval.json").is_file():
            raise ReviewError(
                "diff изменился после одобрения; одобрение неприменимо — "
                "требуется новое сравнение и подтверждение"
            )
    atomic_write(dp_path, patch_text.encode("utf-8"))
    atomic_write(dj_path, canonical_json(diff_json).encode("utf-8"))
    append_event(
        run,
        "helper",
        "diff_created",
        n,
        {
            "diff_sha256": diff_json["diff_sha256"],
            "files_changed": files_changed,
        },
    )
    print(
        f"Diff раунда {n} построен: файлов изменено {len(files_changed)}, "
        f"sha256={diff_json['diff_sha256'][:12]}…"
    )
    return EXIT_OK


# ------------------------------------------------------ record-approval


def cmd_record_approval(root: Path, args) -> int:
    run = root / RUNS_SUBDIR / args.run
    if not run.is_dir():
        raise InputError(f"запуск не найден: {args.run}")
    manifest = load_manifest(run)
    task = load_task_of_run(run)
    n = args.round
    if not (1 <= n <= task["max_rounds"]):
        raise ReviewError(f"раунд {n} вне диапазона 1..{task['max_rounds']}")
    verify_snapshot(run, manifest)
    decisions = load_round_decisions(run, n)
    accepted = accepted_findings(decisions)
    cm_path = round_dir(run, n) / "candidate-manifest.json"
    has_candidate = cm_path.is_file()

    doc = load_json_file(resolve_arg_path(root, args.file))

    # идемпотентность/конфликт до содержательной валидации
    target = round_dir(run, n) / "approval.json"
    if target.is_file():
        existing = load_json_file(target)
        if canonical_json(existing) == canonical_json(doc):
            append_event(
                run, "helper", "idempotent_repeat", n, {"artifact": "approval.json"}
            )
            print("Повторная запись идентичного содержимого: approval.json")
            return EXIT_OK
        append_event(
            run,
            "helper",
            "validation_failed",
            n,
            {"reason": "конфликт: approval.json уже записан с другим содержимым"},
        )
        raise ReviewError("конфликт: approval.json уже записан с другим содержимым")
    problems = []
    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append("schema_version должен быть 1")
    if doc.get("run_id") != args.run or doc.get("round") != n:
        problems.append("run_id/round не совпадают")
    decision = doc.get("decision")
    if decision not in APPROVAL_DECISIONS:
        problems.append(f"недопустимое решение: {decision!r}")
    for field in ("decision_maker", "confirmation_basis", "reason"):
        if not (isinstance(doc.get(field), str) and doc[field].strip()):
            problems.append(f"поле {field} обязательно")
    if doc.get("base_manifest_hash") != sha_file(run / "manifest.json"):
        problems.append("base_manifest_hash не совпадает с исходной базой запуска")
    pa = doc.get("permitted_actions", [])
    if not isinstance(pa, list) or any(a not in PERMITTED_ACTIONS for a in pa):
        problems.append(
            f"permitted_actions: допустимо только {sorted(PERMITTED_ACTIONS)}"
        )

    if has_candidate:
        if doc.get("candidate_manifest_hash") != sha_file(cm_path):
            problems.append("candidate_manifest_hash не совпадает с текущим кандидатом")
        cm = load_json_file(cm_path)
        cand_root = round_dir(run, n) / "candidate"
        for f in cm["files"]:
            p = cand_root / f["path"]
            if not p.is_file() or sha_file(p) != f["sha256"]:
                problems.append(f"кандидат изменён: {f['path']}")
        dj_path = round_dir(run, n) / "diff.json"
        dp_path = round_dir(run, n) / "diff.patch"
        if not dj_path.is_file():
            problems.append("diff не построен; сначала выполните diff")
        else:
            dj = load_json_file(dj_path)
            current_diff_sha = sha_file(dp_path)
            if doc.get("diff_sha256") != current_diff_sha:
                problems.append("diff_sha256 не совпадает с текущим diff")
            if dj.get("diff_sha256") != current_diff_sha:
                problems.append("diff.patch не совпадает с diff.json")
        v_path = round_dir(run, n) / "verification.json"
        if not v_path.is_file():
            problems.append(
                "повторный отчёт (verification) обязателен при наличии кандидата"
            )
        elif doc.get("verification_hash") != sha_file(v_path):
            problems.append("verification_hash не совпадает с записанным отчётом")
    else:
        if not decisions.get("no_fixes_confirmed"):
            problems.append(
                "кандидата нет; требуется no_fixes_confirmed в решениях раунда"
            )
        for field in ("candidate_manifest_hash", "diff_sha256", "verification_hash"):
            if doc.get(field) is not None:
                problems.append(f"{field} должен быть null при отсутствии кандидата")
    if problems:
        append_event(run, "helper", "validation_failed", n, {"reasons": problems})
        raise ReviewError("; ".join(problems))

    if decision == "approve":
        state = STATE_COMPLETED if has_candidate else STATE_COMPLETED_NO_CHANGES
    elif decision == "stop":
        state = STATE_STOPPED
    else:  # revise
        state = STATE_STOPPED if n >= task["max_rounds"] else STATE_IN_PROGRESS

    atomic_write(target, canonical_json(doc).encode("utf-8"))
    append_event(
        run,
        "helper",
        "approval_recorded",
        n,
        {"sha256": sha_file(target), "decision": decision, "run_state": state},
    )
    print(
        f"Решение человека записано: {decision}; состояние запуска: {state}. "
        "Одобрение редакции не равно утверждению правил или приёмке модели."
    )
    return EXIT_OK


# ---------------------------------------------------------------- summary


def cmd_summary(root: Path, args) -> int:
    run = root / RUNS_SUBDIR / args.run
    if not run.is_dir():
        raise InputError(f"запуск не найден: {args.run}")
    manifest = load_manifest(run)
    task = load_task_of_run(run)
    violations = []
    try:
        verify_snapshot(run, manifest)
    except ReviewError as e:
        violations.append(str(e))

    rounds_info = []
    final_state = STATE_IN_PROGRESS
    rd_root = run / "rounds"
    round_dirs = sorted(rd_root.iterdir()) if rd_root.is_dir() else []
    for rd in round_dirs:
        n = int(rd.name)
        info = {"round": n, "artifacts": []}
        review_path = rd / "review.json"
        if review_path.is_file():
            info["review"] = load_json_file(review_path)
            info["artifacts"].append("review.json")
        if (rd / "decisions.json").is_file():
            info["decisions"] = load_json_file(rd / "decisions.json")
            info["artifacts"].append("decisions.json")
        cm_path = rd / "candidate-manifest.json"
        if cm_path.is_file():
            cm = load_json_file(cm_path)
            info["artifacts"].append("candidate-manifest.json")
            for f in cm["files"]:
                p = rd / "candidate" / f["path"]
                if not p.is_file() or sha_file(p) != f["sha256"]:
                    violations.append(
                        f"раунд {n}: кандидат не совпадает с манифестом: {f['path']} "
                        "(возможно, изменён после фиксации/одобрения)"
                    )
        if (rd / "diff.json").is_file():
            dj = load_json_file(rd / "diff.json")
            info["artifacts"].append("diff.patch")
            if (rd / "diff.patch").is_file() and dj.get("diff_sha256") != sha_file(
                rd / "diff.patch"
            ):
                violations.append(f"раунд {n}: diff.patch не совпадает с diff.json")
        if (rd / "verification.json").is_file():
            info["artifacts"].append("verification.json")
        approval_path = rd / "approval.json"
        if approval_path.is_file():
            ap = load_json_file(approval_path)
            info["approval"] = ap
            info["artifacts"].append("approval.json")
            if ap["decision"] == "approve":
                final_state = (
                    STATE_COMPLETED if cm_path.is_file() else STATE_COMPLETED_NO_CHANGES
                )
            elif ap["decision"] == "stop":
                final_state = STATE_STOPPED
            else:
                final_state = (
                    STATE_STOPPED if n >= task["max_rounds"] else STATE_IN_PROGRESS
                )
        rounds_info.append(info)

    lines = []
    lines.append(f"# Сводка запуска {args.run}")
    lines.append("")
    lines.append(
        f"- **Задание:** {manifest['task']['task_id']} (контур R, mode={task['mode']})."
    )
    lines.append(
        f"- **Помощник:** {HELPER_VERSION}; создан {manifest['created_at']}; "
        f"base_commit={manifest['base_commit'] or 'нет (база = снимок и хеши)'}, "
        f"dirty={manifest['base_commit_dirty']}."
    )
    lines.append(
        f"- **Состояние:** **{final_state}** "
        + (
            "(завершение процесса; не утверждение правил и не приёмка модели)"
            if final_state.startswith("completed")
            else ""
        )
    )
    lines.append(
        f"- **Манифест базы:** sha256={sha_file(run / 'manifest.json')[:12]}…, "
        f"файлов: {len(manifest['files'])}, чек-лист {manifest['checklist']['version']}."
    )
    lines.append(
        "- **Среда:** технический read-only не обеспечивается; целостность "
        "гарантируется хешами SHA-256, а не изоляцией среды."
    )
    lines.append("")
    for info in rounds_info:
        n = info["round"]
        lines.append(f"## Раунд {n}")
        lines.append("")
        lines.append("- Артефакты: " + (", ".join(info["artifacts"]) or "нет"))
        if "review" in info:
            s = info["review"]["summary"]
            lines.append(
                f"- Ревью: verdict={s['verdict']}, coverage={s['coverage']}, "
                f"замечаний: {len(info['review'].get('findings', []))}"
            )
            lines.append(
                "- Покрытие чек-листа: "
                + "; ".join(
                    f"{c['item_id']}={c['status']}"
                    for c in info["review"]["checklist_coverage"]
                )
            )
        if "decisions" in info:
            d = info["decisions"]
            acts = {}
            for x in d["decisions"]:
                acts.setdefault(x["action"], []).append(x["finding_id"])
            lines.append(
                "- Решения человека: "
                + "; ".join(f"{k}: {', '.join(v)}" for k, v in acts.items())
                or "-"
            )
        if "review" in info and "decisions" in info:
            lines.append("")
            lines.append("| Замечание | Категория | Строгость | Решение |")
            lines.append("|---|---|---|---|")
            dec_map = {
                x["finding_id"]: x["action"] for x in info["decisions"]["decisions"]
            }
            for f in info["review"].get("findings", []):
                lines.append(
                    f"| {f['id']} | {f['category']} | {f['severity']} | "
                    f"{dec_map.get(f['id'], '—')} |"
                )
        if "approval" in info:
            lines.append("")
            lines.append(
                f"- **Одобрение:** {info['approval']['decision']} "
                f"({info['approval']['decision_maker']}); diff_sha256="
                f"{str(info['approval'].get('diff_sha256'))[:12]}…"
            )
        lines.append("")
    if violations:
        lines.append("## НАРУШЕНИЯ ЦЕЛОСТНОСТИ")
        lines.append("")
        for v in violations:
            lines.append(f"- {v}")
        lines.append("")
        lines.append(
            "Одобрения, привязанные к изменённым артефактам, неприменимы; "
            "требуется новое сравнение и повторное подтверждение."
        )
        lines.append("")
    lines.append("## Ограничения")
    lines.append("")
    lines.append(
        "- Код возврата 0 формальных проверок не является одобрением содержания."
    )
    lines.append(
        "- Помощник не применяет кандидат к рабочим файлам, не выполняет "
        "Git-мутаций и не заменяет решений человека."
    )
    lines.append(
        "- Проверка согласованности текста с мастер-реестром требований "
        "отложена до создания реестра (этапы Д2–Д3 основного плана)."
    )
    summary_text = "\n".join(lines) + "\n"
    atomic_write(run / "summary.md", summary_text.encode("utf-8"))
    if violations:
        append_event(
            run, "helper", "integrity_violation", None, {"reasons": violations}
        )
    append_event(
        run,
        "helper",
        "summary_created",
        None,
        {"final_state": final_state, "violations": len(violations)},
    )
    print(f"Сводка записана: {run / 'summary.md'}; состояние: {final_state}")
    if violations:
        for v in violations:
            print(f"НАРУШЕНИЕ: {v}")
        return EXIT_VIOLATION
    return EXIT_OK


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review.py",
        description=(
            "Локальный помощник рецензирования (контур R): снимки и SHA-256, "
            "формальные проверки, запись решений, diff и сводки. "
            "Код 0 — формальная операция выполнена, НЕ одобрение содержания."
        ),
        epilog=(
            "Коды выхода: 0 — формальная операция выполнена; 1 — нарушения "
            "проверяемого контракта; 2 — ошибки входов/среды."
        ),
    )
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="корень проекта (по умолчанию — каталог, содержащий tools/)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prepare", help="создать запуск по заданию (не перезаписывает)")
    p.add_argument("--task", required=True, help="путь к task.json")
    p.add_argument("--run", required=True, help="идентификатор запуска")

    p = sub.add_parser("check", help="проверить неизменность и формальные условия")
    p.add_argument("--run", required=True)
    p.add_argument("--target", choices=["snapshot", "candidate"], default="snapshot")
    p.add_argument("--round", type=int, default=1)

    p = sub.add_parser("record-review", help="принять отчёт рецензента после валидации")
    p.add_argument("--run", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--phase", choices=["review", "verification"], default="review")
    p.add_argument("--file", required=True, help="путь к отчёту JSON")

    p = sub.add_parser(
        "record-decision", help="записать решение человека по замечаниям"
    )
    p.add_argument("--run", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--file", required=True, help="путь к decisions.json")

    p = sub.add_parser("diff", help="построить diff кандидата относительно снимка")
    p.add_argument("--run", required=True)
    p.add_argument("--round", type=int, required=True)

    p = sub.add_parser(
        "record-approval", help="записать одобрение/решение по кандидату"
    )
    p.add_argument("--run", required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--file", required=True, help="путь к approval.json")

    p = sub.add_parser("summary", help="сводка запуска с проверкой целостности")
    p.add_argument("--run", required=True)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ОШИБКА ВХОДА: корень не найден: {root}", file=sys.stderr)
        return EXIT_INPUT
    handlers = {
        "prepare": cmd_prepare,
        "check": cmd_check,
        "record-review": cmd_record_review,
        "record-decision": cmd_record_decision,
        "diff": cmd_diff,
        "record-approval": cmd_record_approval,
        "summary": cmd_summary,
    }
    try:
        return handlers[args.command](root, args)
    except ReviewError as e:
        print(f"НАРУШЕНИЕ: {e}")
        return EXIT_VIOLATION
    except InputError as e:
        print(f"ОШИБКА ВХОДА: {e}")
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
