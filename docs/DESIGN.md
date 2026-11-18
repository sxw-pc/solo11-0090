# Design notes: deep-pagination drift

## 1. The failure mode

A paginated list is observed as a sequence of windows over a mutable ordered
list. The list is ordered by `sort_key`, and a collection is correct only if:

1. every item present in the starting snapshot is observed at least once;
2. no natural key is observed more than once;
3. observed keys are monotonic in collection order (allowing equal boundary keys);
4. the stopping signal is one of the conditions declared by the plan; and
5. the final observed key reaches the maximum key that existed at collection
   start, or the source is otherwise proven exhausted.

Any mutation between two requests can break one of these invariants.

### Cursor displacement

- **Insert before the current offset cursor.** Later records shift right. The
  next offset window starts at the same numeric position but now includes an
  item from the previous window. This commonly produces a duplicate at the page
  boundary.
- **Delete before the current offset cursor.** Later records shift left. The
  next offset window jumps past one or more records that were never observed.
  This commonly produces a skip.
- **Insert/delete after the cursor** changes what is still to be read but does
  not move past unread items by itself.
- **Change a sort key** can move an item across the cursor. It can disappear
  from the unread region (a skip), be read again at a new key (a duplicate plus
  an out-of-order natural-key jump), or leave a gap in the key range.

The shorthand in many discussions says “front-insert causes skips” and
“deletion causes repeats.” Those are directional intuitions, not reliable
causal labels: the side of the cursor, the number of mutations and the boundary
position all matter. pagedrift therefore classifies findings from evidence, not
from the assumed intent of a mutation. The mixed default schedule deliberately
demonstrates both directions.

### Short pages and false exhaustion

Many clients treat `returned < limit` or an empty page as proof that the list is
exhausted. That is only valid if the source is stable and the client has already
covered everything before the window. A concurrent deletion of the tail can make
the next page short even though the starting snapshot had many later keys. The
collector records four terminal observations:

- final page empty (`saw_empty_page`);
- final page shorter than the requested limit (`partial_page_lt_limit`);
- final observed key reached the source's starting maximum
  (`exhausted_by_max_key`);
- `max_pages` safety stop (never accepted as a proof).

The audit compares those observations to the plan's declared allow-list. A
short page that was accepted can still be a `DRIFT_TAIL_TRUNC` if the starting
snapshot shows unobserved keys beyond the observed maximum.

## 2. Four pagination semantics

| Strategy | Semantics | Safety property |
|---|---|---|
| `offset` | Sort ascending, skip `offset`, take `limit`. | No protection against a mutable prefix. |
| `pagenum` | 1-based page, equivalent to `offset = (page-1)*size`. | Same exposure as offset. |
| `keyset` | Return keys strictly greater than `after_key`, take `limit`. | Stable for already-passed keys; changing keys and deleted tails still cause loss. |
| `snapshot` | First request pins a server-side version and returns a token; all pages read that version. | Immutable view, so it is the safety baseline. |

`keyset` is not automatically safe: an item whose key is moved below the cursor
after being read can be lost, and a tail deletion can still stop collection
early. It is safer than offset for the classic insert-before-cursor failure, but
it is not a completeness proof by itself.

`snapshot` is the baseline because its pages are slices of one immutable list.
If a keyset/offset run reports a defect while the snapshot run is clean, the
defect is attributable to the strategy under mutation. If the snapshot run is
not safe, the mock or the auditor is defective, so the comparison is marked
`inconclusive` rather than blaming a pagination strategy.

## 3. Audit rules

All findings carry `code`, `natural_key`, `page_seq` and `evidence_sha8`.

| Code | Rule | Defect |
|---|---|---|
| `DRIFT_SKIP` | Starting-snapshot item absent from observations, with key in `[initial min, observed max]`. | Yes |
| `DRIFT_DUP` | Natural key occurs more than once; one finding per extra occurrence. | Yes |
| `DRIFT_OOR` | Observed key decreases, or a repeated natural key has a different key from its first observation. | Yes unless `allow_out_of_order` |
| `DRIFT_PREMATURE_TERM` | Starting item with key greater than the observed maximum was never seen when collection stopped. | Yes |
| `DRIFT_TAIL_TRUNC` | Final page empty/short and starting items remain beyond the observed maximum. | Yes |
| `DRIFT_INSERT_OBSERVED` | Observed natural key absent from the starting snapshot and attributed to an insert mutation. | No |
| `RESP_INVALID` | Malformed page, failed run, inconsistent/missing snapshot token. | Inconclusive |

`DRIFT_SKIP` and `DRIFT_PREMATURE_TERM` can both report the same missing record
for different reasons: the first says it fell inside a traversed key range; the
second says it remained beyond the last observed key. One is an interior gap,
the other is an early stop. Both are retained because they support different
remediation.

### Metrics

- `coverage_ratio`: `(distinct initial observed + included extras) /
  (initial count + included extras)`. A collection-time insert is included in
  both numerator and denominator only when `audit.allow_insert_extra` is true.
- `dup_ratio`: duplicate extra occurrences divided by total observed rows.
- `skip_count`: number of `DRIFT_SKIP` findings.
- `skips_per_100_pages`: `skip_count / pages_fetched * 100`, the empirical
  drift-speed estimate for this schedule.
- `findings_by_code`: stable counts for all six drift codes plus `RESP_INVALID`.

### Verdict rules

`inconclusive` if the run failed, an integrity finding exists, or input
fingerprints are incompatible. Otherwise `safe` requires:

- termination proof satisfied;
- zero skips, premature-term and tail-truncation findings;
- no OOR unless `audit.allow_out_of_order` is true;
- `dup_ratio <= audit.max_dup_ratio`.

Everything else is `unsafe`. Comparison adds the snapshot-baseline rule above
and carries plan, universe and schedule SHA-256 values so every conclusion can
be recomputed.

## 4. Evidence and determinism

Each request's raw response bytes are written once to
`pages/<seq>-<sha8>.json`; the full SHA-256 is stored in `requests.jsonl` and in
the ledger. `evidence verify` recomputes hashes and checks both directions
(requests ↔ files, requests ↔ ledger). Runs are append-only: a non-empty run
directory is never reused.

The default plan fixes the dataset seed, mutation schedule and frozen time.
Universe records are serialized in `(sort_key, id)` order with canonical JSON.
HTTP and in-process transports consume the same `MockCore` bytes; elapsed time is
recorded as 0 under `--frozen-time`. Reports contain no absolute paths, so the
same plan/schedule produces the same numbers, findings and JSON bytes in either
transport.

## 5. Fictional election seat settlement

The `election` feature is a fully synthetic contract algorithm for the invented
`fictional-seven-rivers` jurisdiction. Every vote, party name, district and
majority conclusion is locally generated; it deliberately does not model any
real election system or political fact.

### 5.1 Evidence lock

`inspect_election_run` only accepts a run whose audit verdict is `safe` and
whose page evidence still verifies. The run's unique item ids must exactly
cover the fixture's precinct identities: duplicated or missing precincts,
unknown parties and invalid certified direct winners all block inspection. The
returned evidence binds the run id, audit fingerprint and fixture SHA-256.
`preview_national_list_seats` is a plain national D'Hondt preview using exact
`Fraction` values; it never merges direct seats, so it can contradict the
certified direct lower bounds without settling anything.

### 5.2 Two pre-fixed contracts

Both contracts consume the same verified evidence and emit hash-stamped
receipts (`pagedrift/seat_settlement.py`).

1. **fixed_house** — the chamber keeps exactly `base_house_seats` seats. All
   certified direct winners are retained first; a national preview that would
   give a party fewer seats can never revoke a direct seat. Remaining seats are
   filled one at a time by the highest next D'Hondt quotient computed against
   each party's *current total* seat count, with exact quotient ties resolved
   by ascending party id. Every round records all parties' quotients, the
   selected party and the reason.
2. **overhang_expansion** — direct seats are kept but the chamber may grow.
   The contract evaluates every house size from `base_house_seats` upward,
   recording the full national allocation and precise shortfalls at each
   infeasible N, and stops at the first N whose national D'Hondt allocation
   dominates every party's direct count. List top-up seats are the minimum-N
   national allocation minus the direct lower bound, so direct and national
   seats are never added together. A zero-vote party holding a direct seat is
   structurally never feasible and raises `NeverFeasibleSettlementError`.

Because the two contracts are both legitimate pre-fixed rules over identical
inputs, they can reach opposite single-party-majority conclusions: the fixed
chamber may leave one party above half while the expanded chamber dilutes the
same party below half. Receipt replay recomputes both contracts and refuses any
receipt whose fingerprint or run/audit/fixture binding no longer matches.

