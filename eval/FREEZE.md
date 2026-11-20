# HELD-OUT freeze record

The HELD-OUT cases in `eval/cases.jsonl` were frozen before any tuning. `python -m eval.freeze` prints the
sha256 of the held-out cases (canonical JSON, sorted by id). `tests/test_eval_cases.py` fails unless that hash
equals the **last** `HELDOUT sha256=` line below. To change a held-out case you must add a new row here,
with the new hash and the reason (an "unfreeze" decision).

| # | Date | Hash | Reason |
|---|---|---|---|
| freeze-1 | 2026-11-20 | `HELDOUT sha256=02451a7c857994a01a9438cb90533cace5bed4a0b912ba94cc9450e8b77383a7` | initial freeze (37 cases drafted, before any retrieval or threshold tuning) |
| unfreeze/refreeze-2 | 2026-11-20 | `HELDOUT sha256=03f8a104ff18310835c6c6dc8bfef912219cab25cbd1ea1aba2ce4a62ec345e7` | case-set revision before any tuning: +7 HELD-OUT (2 multi-section, 2 boundary, 2 leak-bait, 1 Hinglish), +11 DEV. The 37 original cases are byte-identical (their held-out subset still hashes to freeze-1) |
