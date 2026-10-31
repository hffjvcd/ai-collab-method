# Ledger template

A ledger is append-only. One line per review event, one line per irreversible
action, one line per rule violation. The point is to make the decision
reconstructible later, not to produce a pretty report.

> Everything on this page is fictional. Names, dates, ids and numbers are
> invented to show the shape of the data.

## 1. Review ledger (one row per review)

| date | topic | tier | mode | real findings | user decision | est. cost |
|---|---|---|---|---|---|---|
| 2020-01-01 | rename a config key | 2 | cross-review | 1 | execute | 1 unit |
| 2020-01-02 | replace the task scheduler | 1 | independent plan | 3 | defer | 3 units |
| 2020-01-03 | fix a watchdog false positive | 2 | cross-review | 0 | execute | 1 unit |

- **tier 1** — hard to reverse (delete data, reinstall, swap the main toolchain).
- **tier 2** — everything else that still benefits from a second reader.
- **mode** — `cross-review` (reviewer sees the proposed plan) or
  `independent plan` (reviewer sees only the goal and evidence).

## 2. Irreversible-action log (one row per action)

| time | action | category | reviewed? | changed by review? |
|---|---|---|---|---|
| 2020-01-01 10:00 | drop a cache directory | data | no | n/a |
| 2020-01-02 11:30 | re-register scheduled tasks | system | yes | no |
| 2020-01-03 09:15 | rotate an API key | credential | yes | yes |

## 3. Violation log (append-only, never edited)

| date | clause | what happened | found by | occurrence |
|---|---|---|---|---|
| 2020-01-02 | R3 | a summary dropped 3 of 6 findings | user | 1 |
| 2020-01-04 | R5 | opinion section restated the review conclusion | author | 1 |

## 4. Machine-readable form

The audit tools in `scripts/` read a JSONL ledger. One object per line:

```json
{"id":"0000000000aa","ts":"2020-01-01T10:00:00+00:00","actor":"automation","action":"patch","skill":"example-skill","evidence":{"request":"user asked for a rename"},"before":[{"path":"/skills/example-skill/SKILL.md","sha256":"0000"}],"after":[{"path":"/skills/example-skill/SKILL.md","sha256":"1111"}]}
{"id":"0000000000ab","ts":"2020-01-01T10:00:05+00:00","actor":"automation","action":"patch","skill":"example-skill","evidence":{},"before":[{"path":"/skills/example-skill/SKILL.md","sha256":"1111"}],"after":[{"path":"/skills/example-skill/SKILL.md","sha256":"2222"}]}
```

Fields the tools rely on: `id` (12 hex chars), `ts` (ISO 8601), `skill`,
`action`, `evidence`, and `before` / `after` file lists with `path` and
`sha256`. Content blobs live outside the ledger, addressed by sha256.

The two lines above intentionally show a two-patch burst on one skill: that is
what red line 3 looks for.
