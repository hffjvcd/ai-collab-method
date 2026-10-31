# Ledger audit red lines

Five rules for reviewing automated writes into a skill ledger. Any single hit is
a finding. The audit script passes this whole file to the reviewing model as the
judging criteria.

---

1. A hard clause in a rules-bearing skill is relaxed or removed (for example
   "read-only / block / forbidden" turned into "suggested / optional").

2. Instructions, references or examples are deleted, or a path / command / key
   name is written incorrectly.

3. The same skill is patched several times in a very short window. This usually
   means the writer is oscillating or self-correcting; find the root cause
   instead of accepting the last patch.

4. Content of a pinned or hand-written skill is damaged.

5. The ledger contains a create / delete / archive event with no explanation.

---

## How to read the rules (operating notes, not red lines)

- **Rules-bearing skill**: any skill whose name or body is about rules,
  collaboration boundaries, review, speaking rights or retelling.
- **Short window**: the script uses two patches to the same skill within 60
  seconds as the candidate signal. The model makes the final call.
- **Unexplained**: archive events produced by routine age-based lifecycle
  management are normal. Automated create or remove events need a corresponding
  user request or content change in their evidence field.
- **Rollback**: single-entry rollback is fail-closed and reversible. Whole-tree
  rollback needs a fresh snapshot first.
- **During an audit, do not compact the ledger**: compaction deletes entries and
  garbage-collects content blobs, which destroys the material under review.

---

## Already adjudicated (fictional example)

The format below is what an entry looks like once a hit has been explained. It
is an example only; the id and dates are invented.

- `0000000000aa` (entry 12, 2020-01-01): a quota line changed from
  "8 per month" to "no limit". The user explicitly lifted the limit on the same
  day, and the authorization is recorded in the decision log. This is an
  authorized change; a missing authorization trace in the ledger is not by
  itself an overreach.
