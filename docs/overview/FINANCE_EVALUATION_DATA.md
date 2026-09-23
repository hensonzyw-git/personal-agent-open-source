# Public Finance evaluation data

`evals/finance_public_synthetic_v1.jsonl` contains 73 offline contract cases: 72 synthetic cases and one PRD example. The 20 `PUB-*` cases were created for publication. They use invented inputs, amounts, names and expected outputs, and are not user-reviewed observations. The private real-user cases and their digest witnesses are not distributed.

The linter checks the case schema, tool contract and provenance claims. A passing lint run does not measure model accuracy. Results from the private dataset must not be compared directly with results from this public corpus.

`TRV-005` tests classification of transport spending during a fictional trip. Its place, date, amount and phrasing were recreated for publication. Synthetic cases that claim live, private or real-user provenance are rejected by the linter. This metadata guard does not prove every case's origin; the [verification record](../verification.md) describes the source comparison and its limits.
