# A6 matcher design

How Track A links bank credits to invoices (`ledger.matches`) and writes the payment-timing
labels (`labels.invoice_outcomes`). Code: `platform/src/matsya_platform/match/`. This is a
separate file because `docs/track-a-contract-positions.md` was getting long; contract gaps
found while building the matcher are recorded there as F items.

## The problem and why it stays small

This is cash application, also called receivables line-item matching. The general bundle
case (one credit paying an unknown subset of open invoices) is the subset sum problem,
which is NP-complete. At MSME scale that does not matter: a buyer has fewer than about 15
open invoices at a time, so bundles are bounded and brute-forced exactly instead of
approximated.

Our problem is also easier than the Western literature in one important way. There, a
customer can short-pay by any amount for any reason. Here, the main reason a credit falls
short of gross is TDS, and TDS comes from a small, discrete statutory set of rates applied
to one of two bases. So "why is this credit short?" usually has a finite list of exact
answers, not a continuous residual.

## Cascade

Each stage removes what it resolves from the pool, so later stages see a smaller problem.

**Stage 0, buyer resolution and candidate pool.** The credit's buyer comes from the
narration: a checksum-valid GSTIN, then the PAN inside a GSTIN (characters 3 to 12), then
the exact buyer name, then a unique close name match. The path is recorded, and weaker
paths carry lower confidence through to the match. Credits on a TReDS platform rail name
the platform, not the buyer; their candidates are the invoices of TReDS-eligible buyers
(CPSE, or `treds_mandated`). A credit with no buyer, no platform rail and no invoice
reference is not treated as a receipt at all (refunds, misc income, owner transfers) and
never enters the review set. Candidates for an invoice are credits from its buyer with
`value_date` in `[issue_date, issue_date + W]` (W configurable, default 120 days) and
`known_at <= as_of`.

**Stage 1, reference match.** Invoice references parsed from the narration (Track B's
world puts the invoice number in about 70 percent of payment narrations). If a reference
resolves to an open invoice of the right buyer and the amount is explicable (gross, gross
minus a TDS hypothesis, a TReDS discount, or a part-payment), the match is accepted with
high confidence, `method = reference`. The reference fixes which invoice; arithmetic still
has to explain the rupees. `ledger.invoices` stores only the hashed `invoice_id`, so a
parsed reference is resolved by hashing its spelling variants for the plausible fiscal
years (F8).

**Stage 2, exact amount one to one.** Accepted only if exactly one open invoice of the
buyer fits within tolerance. Two or more fits are not accepted here; they fall through to
the confidence stage, which sends them to review.

**Stage 3, TDS-explained amount.** For each candidate invoice, each rate in a configurable
set that includes 0 percent, and each basis in {taxable value, gross}:
`expected = gross - rate * basis`. A credit is compatible when the difference is within a
configurable tolerance. `tds_inferred_paise`, `tds_rate` and `tds_basis` are recorded.
The base is not a choice the buyer makes: the rule fixes it (see "TDS basis as evidence"),
and the matcher tries both only because which one fits tells it what kind of credit this is.
When the two give the same rupees (no GST on the invoice), the invoice is the same either way,
so this is not ambiguity.

**Stage 4, part-payments.** Cumulative settled = allocated + inferred TDS (+ TReDS fee)
across matches. `first_paid_date` is the first allocation; `paid_date` is the date settled
reaches gross minus tolerance; `paid_fraction = min(settled / gross, 1)`. A part-paid open
invoice has `paid_date` null, `first_paid_date` set and `censored_at` set. A part-payment is
accepted when a reference names its invoice. Without a reference it is never accepted on
"only one open invoice could absorb it": a unique candidate is not arithmetic evidence, and
both wrong links in the first full loop came from that rule. The rule allows a
no-reference part-payment only when its arithmetic is exact under a single TDS hypothesis
and no other open invoice fits at all; a first part always leaves a remainder, so that
never holds, and in practice every no-reference part-payment goes to review. A completing
credit that settles the remainder exactly is a full fit and still matches, which needs
the earlier part to have been accepted.

**Stage 5, bundles.** One credit settling several invoices is subset sum. Bundle size is
capped (default 4) and every subset up to the cap is brute-forced within the buyer's open
invoices (at most the 15 nearest by issue date), applying one TDS hypothesis to the whole
subset. The cap is a correctness guard as much as a performance one: a large bundle that
happens to sum correctly is more likely a coincidence than a real bundle. A bundle is
accepted only when exactly one subset fits exactly, under a single TDS hypothesis
(hypotheses that give the same allocations count as one). If no subset fits, several do,
or one fits under several hypotheses, the credit goes to review.

**Stage 6, TReDS.** A platform-rail credit short of the expected net by more than
tolerance (and by at most a configurable fee share) and arriving before the due date is
`payer_kind = treds_financier`; settled = allocated + TDS + discount fee. A full
expected-net platform credit is `buyer` at any date (pass-through settlement): a financier
pays early only because it discounted, and a discount always arrives short of net. A
short platform credit after the due date is `unknown`. This matters because a
discounted invoice means the supplier got cash while the buyer had still not paid: the
ledger says settled, and the timing label is censored at the discount date. Counting it as
paid on time would teach the forecasting model that the buyer is fast, which is false.

**Stage 7, iterate to stabilise.** Learning each buyer's habitual TDS (rate, basis) makes
results depend on order. It is done EM-style instead: match with uniform priors, estimate
each buyer's habitual (rate, basis) from accepted full matches, re-match with a penalty on
non-habitual hypotheses, and repeat until the accepted set stops changing or the pass cap
is reached (default 3). Credits and invoices are sorted internally, so the same input
gives the same output regardless of input ordering (tested).

**Stage 8, confidence by margin.** For every credit both the best explanation's cost
(unexplained rupees plus penalties) and the gap to the runner-up (a different invoice set)
are computed. A small margin means ambiguity even when the absolute fit is perfect, which
is exactly the two-same-value-invoices case. Routing into the review set is by margin, not
by absolute cost alone.

**Stage 9, outputs.** `ledger.matches` rows and one `labels.invoice_outcomes` version per
invoice at the run, with `first_paid_date`, `paid_date`, `paid_fraction`,
`settlement_kind`, `censored_at` and `known_at`; `censored_at` wins over `paid_date`.
Invoices involved in a review item stay open and censored and are excluded from training
labels. They are never guessed in either direction: defaulting to paid biases the model
toward fast payers, and defaulting to censored biases it the other way. (The contract does
not yet allow `settlement_kind = unknown` on an open label or mark an excluded label, so
for now these invoices get no new label version and are listed in the review set; F11.)

## Advances, contradictions and evidence strength

An advance, a deposit or a round-figure transfer from a real buyer can equal an open
invoice's expected net to the paisa. Arithmetic cannot tell it from a payment. The
mitigations below are downstream signals and better handoff to a person, never a guess about
intent from narration text.

- **Categoriser gate.** A credit that `features.txn_category` labels confidently (at least
  `category_min_confidence`) as anything other than a customer receipt never reaches the
  matcher. No label, `abstain` or low confidence falls back to the receivable test (buyer,
  platform rail or reference). This removes refunds and misc credits, not advances: an
  advance's narration looks exactly like a payment's, so a narration-based categoriser
  calls it a receipt.
- **Settled-invoice contradiction.** When a credit with a resolvable invoice reference
  names an invoice that an earlier credit without a reference would have settled, the
  referenced payment wins. The displaced credit goes to review with the reason
  "settled-invoice contradiction" and a `settled_invoice_contradiction` event (no
  `ops.events` kind yet: F12). This turns an already-correct outcome into an explicit,
  explained one; it does not catch mislinks that no referenced payment ever contradicts.
- **Evidence strength.** Every link is either referenced (`method = reference`) or
  arithmetic-only (`amount_window`, `ip_assignment`), and each labelled invoice is marked
  referenced, arithmetic-only or mixed in the matcher's output. An arithmetic-only link is
  weaker evidence, so a mislinked advance should move a buyer's payment-timing estimate less
  than a referenced payment does. Weakest is `arithmetic_no_basis_signal`: arithmetic-only
  with no TDS basis signal (see "TDS basis as evidence"). `labels.invoice_outcomes` has no field to carry this (F14),
  and it only has effect if Track B uses it.
- **Strictness.** `strictness` decides whether an arithmetic-only link may settle an invoice:
  `permissive` (yes), `balanced` (only when none of the advance-shaped checks fires: the
  credit arrives more than `advance_days_before_due` days before the due date, the amount is
  a multiple of a round step, or the inferred TDS is off the buyer's habit; else review), or
  `strict` (never; every arithmetic-only link goes to review). It is a parameter, not a
  decision; the sweep below gives the trade at each setting. The default is `permissive`
  until a decision is made. In the sweep, `balanced` costs more recall than it saves
  precision at low injection rates (it also catches early real payments) and wins both only
  at high ones, so it is not the default.

## TDS basis as evidence

TDS under the Income Tax Act cannot be calculated on "either" number arbitrarily. It must be
calculated exclusively on the taxable value, excluding GST, provided the GST component is
indicated separately on the invoice. That proviso produces the signal. An advance is paid
before any invoice exists, so there is no separately indicated GST component, the proviso's
condition is unmet, and the deduction falls on the whole amount. A payment against an
invoice that shows GST is taxed on the taxable value; an advance is taxed on the gross. That
asymmetry is the fingerprint, and it follows from the rule, not from buyer discretion. (The
rule was confirmed by the project owner; it is not re-derived here.)

Because the base is fixed by the rule and not chosen by the buyer, a buyer cannot mix bases
for the same kind of transaction. A buyer who uses taxable value on invoice payments and the
gross amount on advances is the rule operating correctly, not inconsistency, so the
"mixed-basis buyer" risk is closed by the rule and has no separate fault.

With `basis_evidence` on (the default), an arithmetic-only fit that works only on the gross
basis goes to review with the reason "basis evidence" when the buyer's learned habit is
taxable value. Review, not a cost penalty: an advance usually fits exactly one invoice, so
there is no runner-up for a penalty to lose to, and a penalty large enough to matter would
be a review trigger in disguise. The habit requirement keeps the read sound: the buyer's own
accepted fits establish its practice first, and with no habit known nothing is flagged.

The basis says nothing in two cases, and a review item arising from either says so in its
reason and carries `basis_signal` in its evidence, so the owner sees why the system could
not decide:
- **0 percent TDS.** Both bases give the same rupees.
- **Rate x GST within the basis tolerance.** The two deductions differ by rate x GST. The
  basis test has its own tolerance, `basis_tolerance_paise` (5 paise), separate from the
  Rs 2 amount tolerance. The amount tolerance absorbs rounding and bank charges; at 0.1
  percent on a small invoice the gap between the bases is under Rs 2, so judged by the
  amount tolerance it would be swallowed. Rounding moves the gap by at most 1 paisa, so 5
  paise sits well below the smallest realistic gap and above the noise. Only an invoice
  with GST under Rs 50 at 0.1 percent falls inside it.

A link resting on arithmetic with no basis signal is weaker evidence, and its label is marked
`arithmetic_no_basis_signal` (F14) so Track B can weight it down. It still settles the
invoice.

Bundles are out of scope for the basis read: across several invoices the basis is
ambiguous, the general case is subset sum (NP-complete), and bundles already go to review.

Measured (coincidental_credit sweep, 4 worlds x 3 seeds, advances taxed on gross): the
basis signal caught 103 of 114 advances that would otherwise have settled an invoice at 5
percent injection, and 416 of 463 at 25 percent; nothing else caught them. Judged by the
amount tolerance, 3 more slipped through: one invoice (gross Rs 11,181.26, GST Rs 1,705.62,
buyer at 0.1 percent with a learned taxable-value habit from 40 links) whose two bases differ
by 170 paise. With the separate basis tolerance they are caught (419 caught at 25 percent),
and the 46 still linked all carry 0 percent TDS, every one labelled
`arithmetic_no_basis_signal`. With no advances present it flagged no real
payment and cost no recall; recall
improves when advances are present, because the invoices they would have taken stay open
for the real payments.

## Escalate rather than guess

The general bundle and part-payment cases have no unique arithmetic answer, so the matcher
escalates them to review instead of guessing. This is deliberate and matches the industry
pattern of escalating below a confidence threshold. It trades recall for precision on
purpose: a wrong link teaches the timing model a wrong paid date, while an unmatched credit
only leaves an invoice censored. Review-set size is the metric that keeps that trade
honest; if it grows past about 5 percent of credits, the bar is too high or the data has
changed.

## Review set

Data only, no UI (`match/review.py`). Items are ranked by the cost of getting them wrong:
at-risk rupees x (1 + days the paid date would move / 30) x 1 / sqrt(1 + n), with n the
buyer's invoices already labelled paid, since a bad label on a buyer with three invoices
distorts far more than on one with forty. Each item carries the evidence the matcher
computed: the ranked candidate explanations with unexplained rupees, TDS rate and basis
under each, days relative to the due date, whether the rate matches the buyer's habit, and
whether this buyer's links usually carry a reference when this credit does not. Candidates
the matcher cannot tell apart are reported as tied, both at rank 1, never ordered one first.
Items sharing a buyer and a cause are grouped as one decision. The quality metric is the
share of at-risk rupees covered by the top 5, 10 and 25 items: an owner who works 10 items
and covers most of the risk has a working system even with 60 in the queue. In production, owner
corrections are the only ground truth; they land in `labels.txn_corrections`. Review-queue
size is a quality metric to keep under about 5 percent of credits, not a feature to expand.

## Limitations (stated plainly)

- **Earlier precision did not speak to advances.** The 1.000 link precision was measured on
  a world that never produces a same-buyer non-invoice credit at an exact invoice net. Under
  the `coincidental_credit` fault with advances taxed on gross (4 worlds x 3 seeds), link
  precision with `basis_evidence` on is 0.991 to 1.000 at 5 percent injection and 0.977 to
  0.997 at 25 percent. Without the basis signal (the baseline before it existed) it is 0.96
  to 0.99 at 5 percent and 0.87 to 0.91 at 25 percent.
- **Some mislinks cannot be detected at all.** When neither the advance nor the real
  payment carries a reference, nothing contradicts the link. It surfaces only as
  unexplained cash in aggregate.
- **Review-set size and unexplained credit rupees are scoring-only today.** They are the
  only signal for the mislinks no referenced payment ever contradicts, and nothing in
  production computes or alerts on them yet (F13).
- **Why an advance bites, now instrumented.** For each injected credit the fault records
  whether its target invoice's real payment exists, was delivered by the cut, and carried a
  reference. On the main world at 25 percent injection: 0 of 72 bite when the real payment
  was delivered with a reference; all 26 bite when it was delivered without one; all 5 bite
  when it had not been delivered by the cut; all 6 bite when the invoice was never paid.
  The bite rate is therefore the share of targets with no referenced real payment in the
  ledger at the cut, not reference coverage alone; the earlier attribution to reference
  coverage was incomplete.
- **The fault is a stress parameter, not an observed frequency.** The injection rate and the
  exact-fit design are chosen to probe the failure. Nobody has measured how often a real MSME
  receives a same-buyer non-invoice credit at an exact invoice net.
- **Some advances are invisible to the basis signal.** About 10 to 12 percent of injected
  advances carry 0 percent TDS, and a few more have rate x GST within tolerance; arithmetic
  gives no basis evidence for them. Under `permissive` they settle the invoice; under
  `balanced` or `strict` they reach review with a reason naming the blind spot.
- **The categoriser gate is measured against an oracle.** In the loop it uses the true
  category with confidence 0.9 (injected advances labelled as receipts), so the review-set
  reduction it shows is an upper bound on what Track B's categoriser will deliver.

## Roadmap

- For MSMEs whose bank or payment provider issues virtual account numbers, each buyer or
  invoice gets its own account number and the payment arrives already identified, so this
  ambiguity disappears at source; the matcher exists for everyone else. Nothing is built
  for it.

## Rejected, and why

- **SAP-style invoice-invoice cliques and super-invoices.** Needed at enterprise scale,
  with thousands of open invoices per customer. Unnecessary when a buyer has under about 15.
- **A MILP or CP-SAT solver.** Brute force within the cap is exact and instant at this scale.
- **Remittance-advice PDF or email extraction.** We have no such document and no connector
  for one. Roadmap, not now.
- **Any LLM in the matching decision.** Extraction may propose candidates in future;
  arithmetic always judges. No model decides a rupee figure.
- **Cost-function tuning of tolerance thresholds.** Worth doing once we have real
  review-cost data. Thresholds are tuned by hand for now (`match/config.py`).

## Constants to verify

- The TDS candidate rates (0, 0.1, 1, 2 and 10 percent) are marked `# VERIFY:` in
  `match/config.py`. The basis rule is confirmed and is not marked.
- The maximum plausible TReDS discount fee share is an assumption, not a sourced figure.
