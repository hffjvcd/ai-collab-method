# ai-collab-method

> A collaboration protocol for two AI agents (or one human plus one agent) where neither side can be trusted to check itself.

Two language models can make the same mistake and agree about it. This protocol
therefore does not rely on "both said it is fine". It relies on **verification
after execution, plus a rollback path**.

## What it solves (in plain words)

1. A second model agrees with the first one, and the agreement is mistaken.
2. The agent summarizing the reviewer quietly drops half the findings, reorders
   them, and adds "this is the most important one".
3. A "review" that only confirms the plan is indistinguishable from no review.
4. Nobody can reconstruct later why a decision was made.

## Quick start

1. Read [`docs/dual-engine-review.md`](docs/dual-engine-review.md). It is the
   full protocol, about 15 minutes.
2. Copy [`docs/ledger-template.md`](docs/ledger-template.md) and start the
   ledger before your first review. Write the plan first, the result after.
3. Paste the task template at the end of the protocol into your first review.
4. If you run a skill ledger, the two scripts in `scripts/` audit automated
   writes against [`docs/audit_rules.md`](docs/audit_rules.md).

## The three ideas worth stealing

**Review is read-only, execution is post-verified.**
The reviewer never touches anything. The executor does not act until the user
says so. After acting, the executor verifies. The safety net is verification and
rollback, not consensus.

**Retelling is a verb with rules.**
When the executor passes the reviewer's answer to the user, it may not filter,
reorder, characterize or paraphrase. Labels (`full n/n`, `excerpt k/n`,
`own opinion`) and a separate opinion section make distortion visible at a
glance. This is the part most multi-agent setups are missing.

**Decision points come with recommendations, but recommendations are not
authorization.**
Every option set presented to the user includes a recommendation and one
sentence of reasoning. Accepting a recommendation still requires a separate
"execute" instruction.

## Repository layout

```
ai-collab-method/
├── README.md
├── LICENSE                  # docs: CC BY 4.0 / scripts: MIT
├── docs/
│   ├── dual-engine-review.md   # the protocol (Chinese)
│   ├── audit_rules.md          # five red lines for automated ledger writes
│   └── ledger-template.md      # table shapes + JSONL shape (fictional data)
├── scripts/
│   ├── curator_ledger_report.py  # read-only Markdown diff of a ledger
│   └── audit_ledger_weekly.py    # incremental audit, asks a model to judge
└── cases/
    ├── 016-backup-review.md      # how three real bugs were found
    └── 020-s4u-smoke.md          # how "push succeeded" was actually proven
```

The protocol is written in Chinese. The cases are anonymized: no dates, host
names, user paths, task names or real identifiers.

## The scripts

Both are pure standard library and read the ledger from `$HERMES_HOME` or
`--hermes-home`. They assume a JSONL ledger whose entries carry `id`, `ts`,
`skill`, `action`, `evidence`, and `before` / `after` file lists with `sha256`.
See the template for the exact shape.

```bash
python scripts/curator_ledger_report.py --since-index 100 --out report.md
python scripts/audit_ledger_weekly.py --dry-run     # render, do not call the model
```

`audit_ledger_weekly.py` calls a Claude-compatible CLI (`claude -p`) by default;
set `CLAUDE_CLI` to point at another binary.

## Known limitations

- The protocol assumes a human is in the loop. It is not an autonomous
  multi-agent framework.
- The retelling rules are enforced socially, not technically. The audit script
  checks ledger writes, not whether a summary was faithful.
- The audit script judges with a language model, so it can miss findings. It is
  a filter, not a proof.
- Chinese-language primary text; translations welcome.

## License

Documentation and cases: CC BY 4.0.
Scripts: MIT.
See [LICENSE](LICENSE).

Personal project: issues may go unanswered.
