#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Markdown diff export for the Hermes curator ledger.

Reads (never writes) the Hermes skill ledger and its content-addressed blob
store, and renders a Markdown report of what each ledger entry changed.

    <HERMES_HOME>/skills/.curator_ledger.jsonl
    <HERMES_HOME>/.curator_backups/blobs/<sha256>

Pure standard library. Does not import any Hermes module.

Usage
-----
    python curator_ledger_report.py                        # whole ledger -> stdout
    python curator_ledger_report.py --since-index 367 --out r.md
    python curator_ledger_report.py --skill dual-engine-readonly-review
    python curator_ledger_report.py --since-index 360 --max-entries 25 --summary-only

Exit codes: 0 ok, 2 bad input (ledger missing / unreadable range).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- paths

def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    candidates = []
    if local:
        candidates.append(Path(local) / "hermes")
    candidates.append(Path.home() / ".hermes")
    for c in candidates:
        if (c / "skills").is_dir():
            return c
    return candidates[0]


def default_ledger(home: Path) -> Path:
    return home / "skills" / ".curator_ledger.jsonl"


def default_blobs(home: Path) -> Path:
    return home / ".curator_backups" / "blobs"


# --------------------------------------------------------------------------- ledger

def load_entries(path: Path) -> tuple[list[dict[str, Any]], list[int], int]:
    """Return (entries, malformed_line_numbers, total_lines).

    Malformed lines are skipped but reported so a gap is never silent.
    """
    entries: list[dict[str, Any]] = []
    malformed: list[int] = []
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                malformed.append(lineno)
                continue
            if not isinstance(obj, dict) or "id" not in obj:
                malformed.append(lineno)
                continue
            obj["_index"] = len(entries)
            entries.append(obj)
    return entries, malformed, len(entries)


def read_blob(blobs: Path, sha: str) -> bytes | None:
    if not sha:
        return None
    p = blobs / sha
    try:
        return p.read_bytes() if p.is_file() else None
    except OSError:
        return None


def decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- classification

HARD_TOKENS = (
    "只读", "禁止", "不得", "必须", "不可", "不允许", "严禁", "绝对",
    "read-only", "readonly", "block", "MUST", "NEVER", "forbid", "deny",
)

RULE_SKILL_HINTS = (
    "dual-engine-readonly-review",
    "dual-engine-verification",
    "二审",
    "规则",
    "协作分工",
)

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Whitespace-insensitive form, for the 'pure wording' test."""
    return _WS.sub("", text)


def _hard_count(text: str) -> int:
    low = text.lower()
    return sum(low.count(tok.lower()) for tok in HARD_TOKENS)


def classify_change(before: str | None, after: str | None) -> tuple[str, bool]:
    """Return (label, hard_constraint_weakened).

    label is deliberately coarse: '纯措辞' or '改语义'.
    '纯措辞' = the visible text is unchanged once whitespace is ignored, or the
    change is only a re-ordering of the same lines. Everything else is 改语义.
    """
    if before is None and after is None:
        return "缺内容", False
    if before is None:
        return "新增", _hard_count(after or "") > 0
    if after is None:
        return "删除", _hard_count(before) > 0

    weakened = _hard_count(after) < _hard_count(before)
    if _norm(before) == _norm(after):
        return "纯措辞", weakened

    b_lines = [ln.strip() for ln in before.splitlines() if ln.strip()]
    a_lines = [ln.strip() for ln in after.splitlines() if ln.strip()]
    if sorted(b_lines) == sorted(a_lines):
        return "纯措辞", weakened
    return "改语义", weakened


def unified(before: str | None, after: str | None, path: str, max_lines: int) -> tuple[str, bool]:
    """Unified diff text; second value says whether it was truncated."""
    b = (before or "").splitlines()
    a = (after or "").splitlines()
    diff = list(
        difflib.unified_diff(
            b, a,
            fromfile=("before/" + path) if before is not None else "/dev/null",
            tofile=("after/" + path) if after is not None else "/dev/null",
            lineterm="", n=2,
        )
    )
    if len(diff) > max_lines:
        head = diff[:max_lines]
        head.append(f"... [diff truncated: showing {max_lines} of {len(diff)} lines]")
        return "\n".join(head), True
    return "\n".join(diff), False


def _rel(path: str) -> str:
    """Path relative to the skills dir when possible; keeps reports readable."""
    marker = "\\skills\\"
    if marker in path:
        return path.split(marker, 1)[1]
    marker2 = "/skills/"
    if marker2 in path:
        return path.split(marker2, 1)[1]
    return path


def file_map(entry: dict[str, Any], key: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in entry.get(key) or []:
        if isinstance(item, dict) and item.get("path"):
            out[item["path"]] = item.get("sha256") or ""
    return out


def entry_changes(entry: dict[str, Any], blobs: Path, max_diff_lines: int) -> list[dict[str, Any]]:
    before = file_map(entry, "before")
    after = file_map(entry, "after")

    # A pure relocation (archive/rename) carries the same content, only the
    # paths move. Diffing every file would emit both a full delete and a full
    # add for identical bytes, which is noise and can be megabytes wide.
    if sorted(before.values()) == sorted(after.values()) and before and after:
        return [{
            "path": f"{len(before)} 个文件（路径变化，内容未变）",
            "status": "relocated",
            "label": "纯移动",
            "weakened": False,
            "diff": "",
            "truncated": False,
            "before_sha": "",
            "after_sha": "",
        }]

    changes: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        b_sha, a_sha = before.get(path), after.get(path)
        if b_sha and a_sha and b_sha == a_sha:
            continue  # unchanged file carried in the snapshot
        b_txt = decode(read_blob(blobs, b_sha)) if b_sha else None
        a_txt = decode(read_blob(blobs, a_sha)) if a_sha else None
        if b_sha and b_txt is None:
            b_txt = None  # blob missing: reported below as 缺内容
        if a_sha and a_txt is None:
            a_txt = None
        label, weakened = classify_change(b_txt, a_txt)
        if b_sha and b_txt is None:
            label = "blob 缺失(before)"
        elif a_sha and a_txt is None:
            label = "blob 缺失(after)"
        text, truncated = unified(b_txt, a_txt, _rel(path), max_diff_lines)
        changes.append({
            "path": _rel(path),
            "status": "modified" if (b_sha and a_sha) else ("added" if a_sha else "removed"),
            "label": label,
            "weakened": weakened,
            "diff": text,
            "truncated": truncated,
            "before_sha": b_sha,
            "after_sha": a_sha,
        })
    return changes


def is_rule_skill(skill: str) -> bool:
    low = (skill or "").lower()
    return any(h.lower() in low for h in RULE_SKILL_HINTS)


def jitter_ids(entries: list[dict[str, Any]], window_secs: int = 60) -> set[str]:
    """Entry ids that belong to a >=2-patch burst on the same skill."""
    bursts: set[str] = set()
    groups: dict[str, list[tuple[_dt.datetime, dict[str, Any]]]] = {}
    for e in entries:
        if e.get("action") != "patch":
            continue
        ts = parse_ts(e.get("ts"))
        if ts is None:
            continue
        groups.setdefault(str(e.get("skill") or ""), []).append((ts, e))
    for items in groups.values():
        items.sort(key=lambda x: x[0])
        for i in range(1, len(items)):
            if (items[i][0] - items[i - 1][0]).total_seconds() <= window_secs:
                bursts.add(str(items[i][1].get("id")))
                bursts.add(str(items[i - 1][1].get("id")))
    return bursts


def parse_ts(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------- render

def render(entries: list[dict[str, Any]], blobs: Path, *, start: int, end: int,
           max_diff_lines: int, summary_only: bool) -> str:
    generated = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    bursts = jitter_ids(entries)
    out: list[str] = []
    out.append("# Ledger - change diff report")
    out.append("")
    out.append(f"- 生成时间：{generated}")
    out.append(f"- 条目区间：[{start}, {end})  共 {len(entries)} 条")
    out.append(f"- blob 目录：`{blobs}`")
    out.append("")
    out.append("标记口径：**纯措辞** = 忽略空白后文本不变或仅行序变化；**改语义** = 其余；"
               "**⚠硬约束** = 只读/禁止/必须等强约束词出现次数减少。")
    out.append("")
    out.append("## 概览")
    out.append("")
    out.append("| # | 时间(本地) | id | actor | action | skill | 文件数 | 标记 |")
    out.append("|---|---|---|---|---|---|---|---|")
    details: list[str] = []
    for e in entries:
        idx = e["_index"]
        ts = parse_ts(e.get("ts"))
        ts_local = ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") if ts else str(e.get("ts"))
        skill = str(e.get("skill") or "")
        changes = entry_changes(e, blobs, max_diff_lines)
        labels = sorted({c["label"] for c in changes})
        tags: list[str] = []
        if is_rule_skill(skill):
            tags.append("规则类")
        if str(e.get("id")) in bursts:
            tags.append("⚠抖动")
        if any(c["weakened"] for c in changes):
            tags.append("⚠硬约束")
        if not changes:
            tags.append("无内容变化")
        tags.extend(labels)
        out.append(
            f"| {idx} | {ts_local} | `{e.get('id')}` | {e.get('actor','')} | "
            f"{e.get('action','')} | {skill} | {len(changes)} | {' / '.join(tags)} |"
        )
        if summary_only:
            continue

        details.append("")
        details.append(f"### [{idx}] `{e.get('id')}` · {ts_local} · {e.get('actor','')} / {e.get('action','')} · {skill}")
        ev = e.get("evidence") or {}
        if ev:
            details.append(f"- evidence: `{json.dumps(ev, ensure_ascii=False)}`")
        if not changes:
            details.append("- （before/after 文件集合相同或 blob 不可读）")
            continue
        for c in changes:
            flag = " · ⚠硬约束" if c["weakened"] else ""
            details.append(f"- `{c['path']}` — {c['status']} — {c['label']}{flag}")
            if c["diff"]:
                details.append("")
                details.append("```diff")
                details.append(c["diff"])
                details.append("```")
    out.extend(details)
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hermes-home", default=None, help="Hermes home (default: $HERMES_HOME or %%LOCALAPPDATA%%\\hermes)")
    p.add_argument("--ledger", default=None, help="override ledger path")
    p.add_argument("--blobs", default=None, help="override blob directory")
    p.add_argument("--since-index", type=int, default=0, help="first ledger index to include (0-based)")
    p.add_argument("--until-index", type=int, default=None, help="exclusive upper ledger index")
    p.add_argument("--max-entries", type=int, default=None, help="cap the number of entries rendered")
    p.add_argument("--skill", action="append", default=None, help="only this skill (repeatable)")
    p.add_argument("--actions", default=None, help="comma list: patch,create,write_file,remove_file,archive")
    p.add_argument("--max-diff-lines", type=int, default=200, help="per-file diff line cap (default 200)")
    p.add_argument("--summary-only", action="store_true", help="overview table only, no diffs")
    p.add_argument("--out", default=None, help="write markdown here instead of stdout")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    home = Path(args.hermes_home).expanduser() if args.hermes_home else hermes_home()
    ledger = Path(args.ledger) if args.ledger else default_ledger(home)
    blobs = Path(args.blobs) if args.blobs else default_blobs(home)

    try:
        entries, malformed, total = load_entries(ledger)
    except FileNotFoundError:
        print(f"ledger not found: {ledger}", file=sys.stderr)
        return 2

    start = max(0, args.since_index)
    end = total if args.until_index is None else min(total, args.until_index)

    selected = [e for e in entries if start <= e["_index"] < end]
    if args.skill:
        wanted = {s.lower() for s in args.skill}
        selected = [e for e in selected if str(e.get("skill") or "").lower() in wanted]
    if args.actions:
        wanted_actions = {a.strip().lower() for a in args.actions.split(",") if a.strip()}
        selected = [e for e in selected if str(e.get("action") or "").lower() in wanted_actions]
    if args.max_entries is not None:
        selected = selected[: max(0, args.max_entries)]

    text = render(selected, blobs, start=start, end=end,
                  max_diff_lines=max(10, args.max_diff_lines),
                  summary_only=args.summary_only)
    if malformed:
        text += f"\n> 警告：跳过了 {len(malformed)} 行无法解析的账本记录（行号 {malformed[:10]}）。\n"

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(str(out))
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
