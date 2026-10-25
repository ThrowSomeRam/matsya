# Owner dashboard: payload gaps (asks for Track B)

The owner page (`dashboard/owner`) was redesigned around three questions: am I OK, who is the
problem, what do I do this week. All of its logic is in `dashboard/owner/src/viewmodel.ts`, which
reads the OwnerView payload (`present/owner_view.py`, schema `owner-view-v1`) and nothing else.
`intelligence/` was not changed.

Some rules need facts the payload does not carry. For each one below: what is missing, why it
matters to the owner, and what the dashboard does now. Workarounds are named in the code, so they
can be deleted once the field exists.

## 1. Supplier names

- **Missing:** a plan step's stretch target is `S1:+30d`. The supplier's name is not in the payload
  and there is no supplier table (`ledger.purchase_invoices` is deferred).
- **Why:** "Ask Supplier S1 to wait 30 more days" is an instruction nobody can act on without a
  lookup.
- **Now:** `supplierLabel` shows "Supplier S1" from the id and never invents a name.

## 2. An explicit MSME-supplier flag

- **Missing:** whether a stretched supplier is micro or small under the MSMED Act. The demo script
  knows (`Supplier("S1", 0.5, "micro", 30)`), but the payload does not say.
- **Why:** paying such a supplier late creates a legal liability for the owner: interest at 3 times
  the bank rate, and the 43B(h) deduction moves to a later year. The owner has to see that before
  making the call.
- **Now:** `isLikelyMsmeSupplierFromStretchCost`, marked `DEMO HEURISTIC`, treats a stretch costing
  more than the Rs 2,000 penalty (`decision.stretch` in the constants file) as an MSME supplier.
  It breaks if the penalty changes, or if a non-MSME stretch ever gets another cost. Please add
  `msme_class` to each supplier-stretch action. That also allows labels like "Supplier S1 (micro)",
  which the code already supports.

## 3. Stretch cost breakdown

- **Missing:** a stretch carries one `cost`. In the demo, S1's Rs 1.1 lakh is almost all MSMED
  interest and the deduction delay, not the penalty.
- **Why:** "Estimated cost: Rs 1.1 lakh" for asking a supplier to wait makes no sense to an owner
  unless it says what the money is: interest owed to the supplier, tax paid earlier, or goodwill.
- **Now:** one generic catch sentence ("Paying late can trigger interest under MSMED and may cost
  you a tax deduction"). Please split the cost into `penalty_paise`, `msmed_interest_paise` and
  `deduction_delay_paise`. `decide/costs.py` computes them already.

## 4. A fresh-path-checked figure for the core steps

- **Missing:** each step's `risk_after` is added up step by step on the fresh paths: an additive
  approximation, where only the last step is a full re-run (`decide/plan.py`). There is no checked
  figure for "only the first k steps".
- **Why:** the page shows the steps up to the first one that meets the target as the to-dos, and
  puts the rest under "Optional". In the demo, one step (the S1 stretch) is the to-do and five
  cheap steps are optional. Owners will ask "what if I only do the to-dos?", and the honest answer
  needs a full run.
- **Now:** the page groups steps by the additive figures but never shows them. It shows only the
  fresh-path before and after for the whole plan. Please add a verified `p_short_after` for the
  shortest prefix that meets the target (one extra engine run).

## 5. Attribution that does not overlap

- **Missing:** drivers are neutralised one at a time and mix invoices with buyers. In the demo the
  ten contributions add up to 1.29 against a total risk of 0.34, and Nav Ganesh appears both as a
  bill and as a buyer. The top driver, a bill 320 days overdue with a 100% chance of never being
  paid, is scored on "paid on its due date", which the owner cannot make happen.
- **Why:** any number or bar per driver tells the owner something false about shares of the risk.
- **Now:** the page shows no driver figures. It folds drivers into one row per buyer, uses them
  only for "pays more slowly than similar customers" and for bills unlikely to be paid, and sorts by
  rupees at stake. Please pick one basis (buyer level only, with the buyer's bills nested, or Shapley
  values), so that contributions add up to the total.

## 6. An honest "calibrated" flag

- **Missing:** `FanDay.calibrated` is true for days 1-30 whether or not the calibrator has any
  history. `fan.note` says those days are "checked against what actually happens and adjusted".
  The demo is a first run with no history, so its bands are the engine's own (the decisions doc
  says so; the payload does not).
- **Why:** the claim is untrue for every new tenant, and they are exactly the ones deciding whether
  to trust the page.
- **Now:** the page drops the note and the flag, and says only "estimates, not guarantees".
  Please set `calibrated` only when the band was actually adjusted, and add the number of scored
  outcomes per horizon, so the page can say something like "checked against N past days".

## 7. 180-day scenario views

- **Missing:** `compute_tenant_run` runs every preset and breaking point at `horizon=90`.
  `scenario_presets.yaml` asks for `view_days: [90, 180]` for `reduced_sales`, because the cash
  effect of fewer orders shows only after the receivables lag. At 90 days "orders fall" lowers the
  risk.
- **Why:** showing that would teach the owner that losing sales is safe.
- **Now:** `PRESET_HINTS` copies `view_days` from the presets file, and the page hides any what-if
  that needs a longer horizon than the payload computed. Please run each preset at its
  `view_days`, and carry `view_days` and the computed horizon in each WhatIf.

## 8. The transform type of each what-if

- **Missing:** a WhatIf carries figures (`share`, `amount`, `days`, `breaking_*`) but not the
  transform type or its target. A rate change on buyers and one on orders look the same.
- **Why:** the page turns a breaking point into owner terms by kind. A buyer slowdown factor m
  becomes "customers take about (1/m - 1) longer to pay", which is exact for a constant weekly
  chance and close for the engine's per-week chances. Orders become "orders fall by X". Wording the
  wrong kind would be wrong.
- **Now:** `PRESET_HINTS` maps the two known rate presets by name. Unknown presets with only a share
  are hidden. Please add `transform_type` and `target`.

## 9. Buyer ids and never-paid chances for every open bill

- **Missing:**
  - Owed items and invoice drivers carry a buyer name but no `buyer_id`.
  - Only the top 10 drivers carry `chance_never_paid`.
  - Open amounts per buyer exist only for buyer drivers.
- **Why:** "Who owes you" should list every customer's open total and every bill unlikely to be
  paid, not only those that happen to rank in the top 10.
- **Now:**
  - Rows are merged on the display name.
  - The open total is shown when a buyer driver gives it; otherwise the overdue total.
  - Bills unlikely to be paid are listed only when a driver names them.
  - A chase's "they owe you interest" line is matched by finding the owed buyer's name inside the
    chase sentence.

  Please add a per-buyer block: `buyer_id`, name, open total, overdue total, usual days late, and
  each open bill's never-paid chance.

## 10. What-ifs at more than one size, and sample futures

- **Missing:** each what-if runs at one size: buyers' weekly chance halved, one Rs 2 lakh bill,
  suppliers at 15 days. The breaking-point search evaluates a grid of sizes, then discards it. The
  payload carries daily summaries (5 percentiles), but not a single simulated future.
- **Why:** the dashboard now has a simulation explorer: pick a what-if, then drag or play through the
  90 days. Owners will want to drag the size of the what-if too ("what if they pay 20% slower, not
  twice as slow?"). The engine does not run in the browser, so every size shown has to be computed in
  advance. A few dozen sample futures would let the chart play outcomes one by one, which helps
  people grasp uncertainty better than a band (hypothetical outcome plots).
- **Now:** the slider moves through days only, and a what-if can be switched on or off but not
  resized. Please keep the breaking-point grid in the payload (risk by day and a fan for each size),
  and add about 50 sample balance paths for the baseline.

## 11. Obligations in the payload

- **Missing:** the obligations detector's output (`features.obligations`: salary, rent, EMI, fees, TDS, PF,
  ESI, GST dates and amounts) never reaches the owner view. Neither does the engine's GST per run, or its
  supplier payments.
- **Why:** the dashboard's "Your obligations" and "What you can do" panels need them. They show each
  payment due in the next 90 days, whether it can move (law, salaries, or can ask), and how tight its day
  is. "How tight" means the share of runs whose balance ends that day below the cushion.
- **Now:** `dashboard/owner/precompute/obligations.py` rebuilds the demo tenant's inputs through Track B's
  public functions and reruns the owner view's own baseline (same seed, so the figures match). It writes
  `eval/demo/obligations.json` (schema `obligations-v1`). The dashboard uses that file only when its tenant,
  run and as-of match the owner view. The API has no route for it yet, so the page reads the bundled copy.
  Please add an `obligations` block to the OwnerView, built in `compute_tenant_run` from the same run.
  Then delete the script. Track A: please add `GET /tenants/{id}/obligations`, or serve it inside the owner
  view.
- **Not covered:** no late-payment rate for GST, TDS, PF or ESI is in the constants file as verified, so the
  page says "interest and late fees" without a figure.

## Correction to an earlier note

Earlier notes said the per-step plan figures come from the 1,000-path solve sample. They do not.
They are computed on the 10,000 fresh paths, added up step by step. The solve sample sets only the
order of the steps (leave-one-out importance). Item 4 stands, because the figures are additive
rather than full runs.
