#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weekly (idempotent, incremental) audit of the Hermes curator ledger.


Behaviour
---------
1. Single-instance lock (a second copy exits 0 immediately).
2. Read the cursor from the state file. Missing or corrupt state => audit from
   the beginning. Never skip silently.
3. Entries = ledger[cursor:]. None => exit 0 (empty run, no log noise).
4. If the last SUCCESSFUL audit is younger than 7 days => exit 0 (cooldown).
5. Render a diff report with curator_ledger_report.py and ask `claude -p`
   to judge it against the five red lines in audit_rules.md.
6. Success:
      red lines => popup (30 s auto-close) + alerts.log + desktop file
      clean     => one line in audits.log
   Cursor advances to the ledger tail read at step 3.
7. Failure (report error, claude error, unparsable verdict) => exit 1 and the
   cursor does NOT move, so the same entries are retried next run.

Pure standard library. Does not import any Hermes module.

Exit codes: 0 = ok / nothing to do, 1 = audit failed (cursor unchanged),
2 = setup error (missing rules/report script).
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

TOOL_DIR = Path(__file__).resolve().parent
RULES_FILE = TOOL_DIR / "audit_rules.md"
REPORT_SCRIPT = TOOL_DIR / "curator_ledger_report.py"
STATE_FILE = TOOL_DIR / "state" / "audit_state.json"
LOCK_FILE = TOOL_DIR / "state" / "audit.lock"
LOG_DIR = TOOL_DIR / "logs"
REPORT_DIR = TOOL_DIR / "reports"
AUDITS_LOG = LOG_DIR / "audits.log"
ALERTS_LOG = LOG_DIR / "alerts.log"

COOLDOWN = _dt.timedelta(days=7)
LOCK_STALE_SECS = 6 * 3600
CLAUDE_TIMEOUT_SECS = 1800          # first historical run can take a while
CHUNK_ENTRIES = 30                  # entries per claude call
CHUNK_MAX_CHARS = 120_000           # soft cap for one prompt
MIN_CHUNK_ENTRIES = 1
MAX_ALERTED_IDS = 5000


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


# --------------------------------------------------------------------------- logging

def now_local() -> _dt.datetime:
    return _dt.datetime.now().astimezone()


def stamp(dt: _dt.datetime | None = None) -> str:
    return (dt or now_local()).strftime("%Y-%m-%d %H:%M:%S%z")


def append_log(path: Path, line: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except OSError as exc:  # logging must never kill the audit
        print(f"WARN: cannot write {path}: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- state

def empty_state() -> dict[str, Any]:
    return {"schema": 1, "cursor": 0, "last_success_ts": None, "alerted_ids": [],
            "last_report": None, "last_error": None}


def load_state(path: Path) -> tuple[dict[str, Any], bool]:
    """Return (state, healthy). A missing/corrupt state is NOT fatal: audit all."""
    if not path.is_file():
        return empty_state(), False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_state(), False
    if not isinstance(data, dict):
        return empty_state(), False
    state = empty_state()
    state.update({k: v for k, v in data.items() if k in state})
    try:
        state["cursor"] = max(0, int(state.get("cursor") or 0))
    except (TypeError, ValueError):
        state["cursor"] = 0
    if not isinstance(state.get("alerted_ids"), list):
        state["alerted_ids"] = []
    return state, True


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def parse_ts(value: Any) -> _dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------- lock

def acquire_lock() -> bool:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "ts": stamp()}))
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_FILE.stat().st_mtime
            except OSError:
                age = 0
            if age > LOCK_STALE_SECS:
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    return False
                continue
            return False
    return False


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- ledger

def read_entries(ledger: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with ledger.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("id"):
                entries.append(obj)
    return entries


# --------------------------------------------------------------------------- report

def run_report(start: int, end: int, out: Path, hermes: Path,
               max_diff_lines: int = 200, timeout: int = 300) -> str:
    cmd = [
        sys.executable, str(REPORT_SCRIPT),
        "--since-index", str(start), "--until-index", str(end),
        "--out", str(out), "--hermes-home", str(hermes),
        "--max-diff-lines", str(max_diff_lines),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"report script failed rc={proc.returncode}: {proc.stderr.strip()[:800]}")
    return out.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- claude

def find_claude() -> list[str] | None:
    """Return the argv prefix that runs the Claude CLI, or None."""
    override = os.environ.get("CLAUDE_CLI")
    if override and Path(override).is_file():
        return _argv_for(override)
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    named = Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if named.is_file():
        return [str(named)]
    for name in ("claude.exe", "claude.cmd", "claude"):
        found = shutil.which(name)
        if found:
            return _argv_for(found)
    return None


def _argv_for(path: str) -> list[str]:
    if path.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe"), "/c", path]
    return [path]


def build_prompt(rules: str, report: str) -> str:
    return f"""你是只读审计员。依据下面的「红线」判断台账 diff 报告里是否存在越权写入。

{rules}

--- 报告开始 ---
{report}
--- 报告结束 ---

要求：
1. 只依据报告内容判断，不要臆测报告之外的文件。
2. 对每条命中的红线，必须给出报告表格 `id` 列的值（形如 `a1b2c3d4e5f6` 的 12 位十六进制）。不要用 `#` 列的序号代替。
3. 没有命中就返回空数组。宁可漏报也不要编造 entry id。

只输出一个 JSON 对象，放在回复的最后，不要加解释文字：
{{"red_lines": [{{"entry_id": "<报告里的 id>", "rule": <1-5>, "skill": "<技能名>", "reason": "<不超过80字>"}}], "summary": "<不超过120字>"}}
"""


def call_claude(prompt: str, timeout: int = CLAUDE_TIMEOUT_SECS) -> dict[str, Any]:
    cli = find_claude()
    if not cli:
        raise RuntimeError("claude CLI not found (set CLAUDE_CLI)")
    cmd = cli + ["-p", "--output-format", "json", "--allowedTools", "Read"]
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    proc = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True,
                          timeout=timeout, env=env)
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"claude rc={proc.returncode}: {stderr[:800] or stdout[:800]}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude returned non-JSON stdout: {stdout[:800]}")
    if payload.get("is_error"):
        raise RuntimeError(f"claude reported is_error: {str(payload)[:800]}")
    result = payload.get("result")
    if not isinstance(result, str):
        raise RuntimeError(f"claude json has no result string: {str(payload)[:500]}")
    return {"payload": payload, "result": result, "stderr": stderr}


def _balanced_objects(text: str) -> list[str]:
    """Every top-level {...} substring, by brace depth (strings respected)."""
    out: list[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    out.append(text[start:i + 1])
    return out


def extract_verdict(text: str) -> dict[str, Any]:
    """Pull the verdict JSON object out of the model's answer.

    The model may wrap it in a fence, prefix it with prose, or return it bare;
    nested objects inside red_lines rule out a naive first-brace/last-brace cut.
    """
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    for chunk in [text] + fenced:
        for candidate in [chunk.strip()] + _balanced_objects(chunk):
            try:
                obj = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obj, dict) and "red_lines" in obj:
                return obj
    raise RuntimeError(f"cannot parse verdict JSON from model output: {text[:500]}")


# --------------------------------------------------------------------------- alerting

_SHELL_FOLDERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"


def known_folder(name: str) -> Path | None:
    """Resolve a per-user shell folder (e.g. "Desktop") the way Explorer does.

    The authoritative location is HKCU's User Shell Folders: when the user
    redirects a folder to another drive the value there follows the move,
    while %USERPROFILE%\\Desktop keeps pointing at the untouched default.
    Values are commonly REG_EXPAND_SZ, so expand %VARS% before use.
    Returns None when the registry is unreadable or the value is absent.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS_KEY) as key:
            raw, _kind = winreg.QueryValueEx(key, name)
    except (ImportError, OSError):
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(os.path.expandvars(raw.strip()))


def desktop_dir() -> Path:
    redirected = known_folder("Desktop")
    if redirected is not None:
        return redirected
    home = Path.home()
    for cand in (home / "Desktop", home / "OneDrive" / "Desktop"):
        if cand.is_dir():
            return cand
    return home / "Desktop"


def popup(title: str, text: str, timeout_ms: int = 30_000) -> None:
    MB_TOPMOST = 0x00040000
    MB_ICONWARNING = 0x00000030
    user32 = ctypes.windll.user32
    try:
        # Undocumented but present since XP: auto-closes, never blocks a session.
        user32.MessageBoxTimeoutW(None, text, title, MB_TOPMOST | MB_ICONWARNING, 0, timeout_ms)
    except Exception:
        try:
            user32.MessageBoxW(None, text, title, MB_TOPMOST | MB_ICONWARNING)
        except Exception as exc:  # a headless session must not lose the alert
            print(f"WARN: popup failed: {exc}", file=sys.stderr)


def write_desktop_alert(body: str) -> Path:
    path = desktop_dir() / f"HermesLedger_ALERT_{now_local():%Y%m%d_%H%M%S}.txt"
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- audit

def chunk_ranges(entries: list[dict[str, Any]], start: int, end: int,
                 hermes: Path, chunk_entries: int = CHUNK_ENTRIES,
                 chunk_max_chars: int = CHUNK_MAX_CHARS) -> list[tuple[int, int, int]] | None:
    """Split [start,end) into report chunks under CHUNK_MAX_CHARS.

    Each range carries the diff line cap that made it fit. Multi-entry chunks
    are halved until they fit; a single oversized entry is retried with a
    smaller diff cap instead (its summary line is still auditable).
    Returns None if even one entry cannot be squeezed under the cap.
    """
    ranges: list[tuple[int, int, int]] = []
    pos = start
    while pos < end:
        size = min(chunk_entries, end - pos)
        while True:
            caps = (200, 60, 20, 6) if size <= MIN_CHUNK_ENTRIES else (200,)
            chosen = None
            for cap in caps:
                probe = REPORT_DIR / f".probe_{pos}_{pos + size}_{cap}.md"
                try:
                    text = run_report(pos, pos + size, probe, hermes, cap)
                finally:
                    try:
                        probe.unlink()
                    except OSError:
                        pass
                if len(text) <= chunk_max_chars:
                    chosen = cap
                    break
            if chosen is not None:
                ranges.append((pos, pos + size, chosen))
                pos += size
                break
            if size <= MIN_CHUNK_ENTRIES:
                return None
            size = max(MIN_CHUNK_ENTRIES, size // 2)
    return ranges


def audit(args: argparse.Namespace) -> int:
    hermes = Path(args.hermes_home).expanduser() if args.hermes_home else hermes_home()
    ledger = hermes / "skills" / ".curator_ledger.jsonl"
    if not ledger.is_file():
        append_log(AUDITS_LOG, f"{stamp()} FAIL ledger missing: {ledger}")
        return 2
    if not RULES_FILE.is_file() or not REPORT_SCRIPT.is_file():
        append_log(AUDITS_LOG, f"{stamp()} FAIL missing {RULES_FILE} or {REPORT_SCRIPT}")
        return 2

    entries = read_entries(ledger)
    total = len(entries)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state, healthy = load_state(STATE_FILE)
    cursor = state["cursor"]
    if not healthy:
        append_log(AUDITS_LOG, f"{stamp()} WARN state missing/corrupt -> auditing from entry 0")
        cursor = 0
    if cursor > total:
        append_log(AUDITS_LOG, f"{stamp()} WARN cursor {cursor} > ledger {total} (compacted?) -> reset to 0")
        cursor = 0

    if total <= cursor:
        if args.verbose:
            append_log(AUDITS_LOG, f"{stamp()} EMPTY no new entries (cursor {cursor}/{total})")
        return 0

    last_success = parse_ts(state.get("last_success_ts"))
    if last_success is not None and not args.force:
        age = now_local() - last_success
        if age < COOLDOWN:
            hold = (COOLDOWN - age).total_seconds() / 3600
            if args.verbose:
                append_log(AUDITS_LOG, f"{stamp()} COOLDOWN {age.total_seconds()/3600:.1f}h since last success; "
                                       f"{hold:.1f}h left")
            return 0

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORT_DIR / f"audit_{now_local():%Y%m%d_%H%M%S}_{cursor}-{total}.md"

    ranges = chunk_ranges(entries, cursor, total, hermes, args.chunk_entries, args.chunk_chars)
    if ranges is None:
        append_log(AUDITS_LOG, f"{stamp()} FAIL one entry exceeds report char cap; cursor unchanged at {cursor}")
        return 1

    rules = RULES_FILE.read_text(encoding="utf-8")
    new_ids = {str(e.get("id")) for e in entries[cursor:total]}
    id_to_skill = {str(e.get("id")): str(e.get("skill") or "") for e in entries[cursor:total]}

    findings: list[dict[str, Any]] = []
    summaries: list[str] = []
    full_report_parts: list[str] = []

    for (a, b, cap) in ranges:
        part_path = REPORT_DIR / f".part_{report_path.stem}_{a}-{b}.md"
        try:
            report = run_report(a, b, part_path, hermes, cap)
        except Exception as exc:
            append_log(AUDITS_LOG, f"{stamp()} FAIL report {a}-{b}: {exc}")
            return 1
        finally:
            try:
                part_path.unlink()
            except OSError:
                pass

        if args.dry_run:
            finding = {"red_lines": [], "summary": f"dry-run chunk {a}-{b} ({len(report)} chars)"}
        else:
            prompt = build_prompt(rules, report)
            try:
                raw = call_claude(prompt)
                finding = extract_verdict(raw["result"])
            except Exception as first:
                # One retry with a stricter instruction: a chatty answer must
                # not cost a whole week's cursor advance.
                try:
                    raw = call_claude(prompt + "\n\n重要：上一次回复无法解析。只输出一个 JSON 对象，不要任何其他文字。")
                    finding = extract_verdict(raw["result"])
                except Exception as exc:
                    append_log(AUDITS_LOG, f"{stamp()} FAIL claude chunk {a}-{b}: {first} / retry: {exc}")
                    state["last_error"] = str(exc)[:500]
                    save_state(STATE_FILE, state)
                    return 1

        for item in finding.get("red_lines") or []:
            if not isinstance(item, dict):
                continue
            eid = str(item.get("entry_id") or "").strip()
            # The model sometimes echoes the report's "#" index rather than the
            # id column. Map it back inside this chunk's range instead of
            # discarding a real finding.
            bare = eid.lstrip("#")
            if bare.isdigit():
                idx = int(bare)
                if a <= idx < b:
                    eid = str(entries[idx].get("id") or "")
            if eid not in new_ids:
                append_log(AUDITS_LOG, f"{stamp()} WARN model returned unknown entry id {eid!r}; dropped")
                continue
            item["entry_id"] = eid
            item.setdefault("skill", id_to_skill.get(eid, ""))
            findings.append(item)
        if finding.get("summary"):
            summaries.append(str(finding["summary"])[:500])
        full_report_parts.append(report)

    # persist the full report for humans
    combined = "\n\n".join(
        f"<!-- chunk {a}-{b} -->\n{text}" for (a, b, _cap), text in zip(ranges, full_report_parts)
    )
    report_path.write_text(combined, encoding="utf-8")

    if args.dry_run:
        # A dry run writes the report only: the cursor must not move, or the
        # entries would be marked audited without ever reaching the model.
        append_log(AUDITS_LOG,
                   f"{stamp()} DRY entries {cursor}..{total} | {len(ranges)} chunk(s) | report {report_path.name}")
        print(f"DRY entries {cursor}..{total} -> {report_path}")
        return 0

    # one finding per entry, then drop entries already alerted in a past run
    unique: dict[str, dict[str, Any]] = {}
    for f in findings:
        unique.setdefault(f["entry_id"], f)
    findings = list(unique.values())

    alerted = set(str(x) for x in state.get("alerted_ids") or [])
    fresh = [f for f in findings if f["entry_id"] not in alerted]

    # success: advance the cursor to the tail read at the start of this run
    state["cursor"] = total
    state["last_success_ts"] = now_local().isoformat(timespec="seconds")
    state["last_report"] = str(report_path)
    state["last_error"] = None

    if fresh:
        for f in fresh:
            alerted.add(f["entry_id"])
        state["alerted_ids"] = list(alerted)[-MAX_ALERTED_IDS:]
        save_state(STATE_FILE, state)

        lines = [f"{stamp()} RED_LINES {len(fresh)} new / {len(findings)} total | entries {cursor}..{total} | report {report_path}"]
        body = [f"Ledger audit — 命中 {len(fresh)} 条红线", ""]
        for f in fresh:
            lines.append(
                f"  - [rule {f.get('rule')}] {f.get('skill')} entry={f['entry_id']} :: {f.get('reason','')}"
            )
            body.append(f"红线 {f.get('rule')}｜技能 {f.get('skill')}｜条目 {f['entry_id']}")
            body.append(f"  理由：{f.get('reason','')}")
            body.append("")
        body.append(f"报告：{report_path}")
        body.append(f"处置：先看报告，确认后用 hermes curator rollback <entry-id> 单条回滚。")
        append_log(ALERTS_LOG, "\n".join(lines))

        alert_file = report_path  # fall back to the report path if the desktop is unavailable
        try:
            alert_file = write_desktop_alert("\n".join(body))
        except OSError as exc:
            append_log(ALERTS_LOG, f"{stamp()} WARN desktop alert failed: {exc}")
        if not args.no_popup:
            # A first historical run can flag hundreds of entries; the popup
            # stays readable and the full list lives in alerts.log/desktop file.
            preview = "\n".join(
                f"· {f.get('skill')} [{f['entry_id']}] {str(f.get('reason',''))[:40]}"
                for f in fresh[:8]
            )
            more = f"\n…另有 {len(fresh) - 8} 条，见 alerts.log" if len(fresh) > 8 else ""
            popup("Ledger audit：命中红线",
                  f"命中 {len(fresh)} 条红线，已写入 alerts.log 与桌面。\n\n{preview}{more}\n\n"
                  f"报告：{report_path.name}\n提示文件：{alert_file.name}\n（30 秒后自动关闭）")
        print(f"RED {len(fresh)} -> {report_path}")
        return 0

    save_state(STATE_FILE, state)
    detail = "；".join(summaries)[:300] if summaries else "无摘要"
    append_log(AUDITS_LOG,
               f"{stamp()} CLEAN entries {cursor}..{total} ({total - cursor} 条) | {len(ranges)} chunk(s) | "
               f"report {report_path.name} | {detail}")
    print(f"CLEAN entries {cursor}..{total} -> {report_path}")
    return 0


# --------------------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Incremental weekly audit of the Hermes curator ledger.")
    p.add_argument("--hermes-home", default=None, help="Hermes home (default: $HERMES_HOME or %%LOCALAPPDATA%%\\hermes)")
    p.add_argument("--force", action="store_true", help="ignore the 7-day cooldown")
    p.add_argument("--no-popup", action="store_true", help="do not show the MessageBox")
    p.add_argument("--dry-run", action="store_true", help="render reports but skip the claude call")
    p.add_argument("--verbose", action="store_true", help="log empty/cooldown runs too")
    p.add_argument("--chunk-entries", type=int, default=CHUNK_ENTRIES, help="ledger entries per claude call")
    p.add_argument("--chunk-chars", type=int, default=CHUNK_MAX_CHARS, help="report char cap per claude call")
    return p


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not acquire_lock():
        if args.verbose:
            append_log(AUDITS_LOG, f"{stamp()} LOCKED another run holds {LOCK_FILE}")
        return 0
    try:
        return audit(args)
    except Exception as exc:  # never leave the cursor half-advanced
        append_log(AUDITS_LOG, f"{stamp()} FAIL unexpected: {type(exc).__name__}: {exc}")
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
