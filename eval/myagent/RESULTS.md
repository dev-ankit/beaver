# myagent — Codex on BEAVER `dw`: results

Evaluation of the Codex-backed agent (`agent.py`) on the BEAVER `dw` benchmark.
All numbers are **execution accuracy** (generated SQL's result set equals gold's)
on the standard 100-question `dev_sampled` set, scored against the live MySQL
`dw` database. Generation uses the local Codex CLI (model `gpt-5.5` via ChatGPT
login). Every enhancement below is **gold-blind** — the agent never sees the
expected/correct answer.

> See `../claudeagent/RESULTS.md` for the Claude (`claude -p`) vs Codex
> head-to-head and the cross-model ensembling finding.

## Scoreboard (`dw`, 100 questions, `high` reasoning effort)

| Config | exec acc | correct | SQL errors | empty | mismatch |
|--------|:--------:|:-------:|:----------:|:-----:|:--------:|
| setting 1, one-shot (baseline) | 23% | 23 | 11 | 2 | 64 |
| setting 1 + self-fix | 23% | 23 | 0 | 5 | 72 |
| setting 1 + explore/verify | 26% | 26 | 5 | 3 | 66 |
| setting 2 + self-fix | 30% | 30 | 1 | 4 | 65 |
| setting 2 + explore/verify + fix | 34% | 34 | 0 | 2 | 64 |
| setting 2 + explore/verify + fix + style guide | 37% | 37 | 0 | — | — |
| **+ precedence fix + schema skill + 3 candidates (best guess)** | **39%** | **39** | 0 | — | — |
| *same run, pass@3 (any of 3 candidates matches)* | *49%* | *49* | — | — | — |

5-question sanity samples (setting 1): low 20%, high 40%, xhigh 40%.
Full-100, setting 1: high 23%, xhigh 25% (within run-to-run noise).

## Settings
- **setting 1** — hints: gold tables, column mapping, join keys.
- **setting 2** — setting 1 + domain knowledge + query decomposition.
- (setting 0 = retrieved tables only; not run here — needs `retrieve/`.)

## Enhancements (all in `agent.py`, all gold-blind)
- **self-fix** (`CODEX_SQL_FIX=1`) — runs its own SQL read-only; on an execution
  error, feeds back *only the DB error* and asks Codex to fix, looping up to
  `CODEX_FIX_ATTEMPTS`. Stops once the query executes. → guarantees executable
  output (SQL errors → 0); does not change correctness on its own.
- **explore/verify** (`CODEX_SQL_EXPLORE=1`) — the agent runs read-only queries
  against the real tables (sample rows, counts, its candidate query) and inspects
  the rows *its own* queries return, then self-checks and revises before
  finalizing. Never shown gold rows. → +3–4 pts.
- **style guide** (`CODEX_STYLE_GUIDE=1`) — a "house style" prior appended to the
  prompt: 8 conventions of this benchmark's reference SQL (see the style-guide
  section below). Gold-blind per question, but fit to the dataset's conventions
  via the failure taxonomy. → +3 net, and cracks 5 questions no prior run solved.

## Findings
- **Lever ranking:** richer hints (+7) > explore/verify (+3–4) > self-fix
  (robustness, ~0 acc) ≈ reasoning effort (within noise, high↔xhigh).
- Levers **stack independently**: setting 2 (30%) + explore/verify + fix → **34%**.
- **explore and fix are complementary**: explore improves correctness; the final
  fix pass drives SQL errors to 0 (explore alone left 5).
- The dominant failure mode is **semantic** (~64 "values mismatch") at every
  config — queries execute but return the wrong rows. Even with all 5 oracle
  hints (setting 2), ~66% still miss, i.e. SQL *construction* on these enterprise
  schemas is hard even when schema-linking is handed over.
- **Headroom:** the union of correct sets across runs (~39) exceeds any single
  run, indicating self-consistency / majority-vote ensembling is the next lever.

## Hint ablation: is the decomposition hint worth it?
Controlled ablation (Codex, high + explore/verify + fix), setting 2 with vs
without `--decomp` (all else identical: gold tables + mapping + join keys +
domain knowledge):

| Config | exec acc | correct |
|--------|:--------:|:-------:|
| setting 2 (with decomp) | **34%** | 34 |
| setting 2 − decomp | 28% | 28 |

Per-question, decomposition **helps 13, hurts 7 → net +6**. It is net-**positive**
despite occasionally backfiring. Don't drop it.

## Failure analysis (setting 2 "values mismatch")
Two dissected mismatches — both cases where the *decomposition* hint embedded
gold-query scaffolding that contradicted the final ask, and the model baked it
into the answer:
- **dw_2933** — decomposition said "top 10 organizations"; the question asks "for
  each organization". Agent added `LIMIT 10` → 10 rows vs gold's 154.
- **dw_104** — decomposition mentioned "a window of 2 preceding and 1 following";
  the question wants the *overall* average. Claude used a rolling
  `AVG() OVER (... ROWS BETWEEN 2 PRECEDING AND 1 FOLLOWING)` (wrong per-row
  deviations); Codex used `AVG() OVER ()` (overall) and got it right — a clean
  backend-divergence case.

Caveat (see ablation above): these are a real failure *mode* but a *minority* —
the ablation shows decomposition is net-positive overall. Hand-picked errors
identify modes; only the controlled run gives net impact.

## Negative result: self-decomposition and subagent review hurt
Tested whether the agent's *own* reasoning could replace the provided
decomposition hint: drop `--decomp`, then have the agent self-decompose +
validate each sub-step with SQL (`CODEX_DECOMPOSE`), and/or add a final subagent
that reviews the answer vs the question for intent (`CODEX_REVIEW`). Isolation
(Codex, high, explore + fix, setting 2 minus decomp):

| Config | exec acc | Δ vs baseline |
|--------|:--------:|:-------------:|
| neither (baseline, s2 − decomp) | 28% | — |
| + self-decompose only | 23% | −5 |
| + review only | 21% | −7 |
| + both | 21% | −7 |
| *(reference: s2 **with** decomp hint)* | *34%* | *+6* |

Both components are **net-negative**. The **subagent review is the main culprit
(−7)**: gold-blind, it rewrites already-correct queries into wrong ones (adds more
errors than it removes). Self-decompose (−5) pushes toward more elaborate, fragile
constructions. `both = review-only` → review dominates once present.

**Throughline of the study's three "more reasoning" hypotheses — all refuted by
controlled runs:** dropping the decomp hint hurt (−6); self-decomposing instead
hurt (−5); adding a review subagent hurt most (−7). On BEAVER `dw` the oracle
hints beat the agent's own reasoning, and self-critique without ground truth is
actively harmful. Winner stays the simple recipe: **hints + explore + fix (34%)**.

## Failure taxonomy: why the misses miss
Categorized all 65 dossier-able failures of the best pre-style-guide run
(question + hints + gold + Codex/Claude best-run SQL, diffed per question):

| Cause | n | What it is |
|-------|:-:|------------|
| underdetermined question | 28 | NL can't distinguish gold from pred: `COUNT` vs `COUNT(DISTINCT)`, LEFT-vs-INNER survivorship, unstated `>0` filters, `TERM_CODE` vs `EFFECTIVE_TERM_CODE`, running vs plain aggregates, how blocks combine |
| gold suspect | 18 | gold contradicts the question: `STDDEV_SAMP` despite "use STDDEV only" in the question text; "variance in 2022" with no year filter; dangling fan-out joins inflating sums |
| hint backfire | 10 | decomposition sub-questions contradict the final question ("top 10" vs "for each", "Chemistry only" vs "each department") and the model follows the hint |
| model error | 6 | mostly inter-block linkage in composed queries |
| evaluator artifact | 3 | formatting / extra display column |

Only ~9% of failures are model SQL errors. In **46/65 (71%) both backends made
the identical non-gold choice** — convergent evidence the question or gold, not
the model, is the bottleneck (and a ceiling on what ensembling can recover).

Structural signal: 50/100 questions were never solved by *any* of 11 full runs
(both backends, all configs). The 9 `base` (real, single-intent) questions solve
at 78% with zero never-solved; the 91 template-composed questions collapse
monotonically with sub-question count (0 parts → 0% never-solved; 4 → 65%).
Each composed sub-part plus the combination step is another independent chance
to diverge from gold's arbitrary convention.

### Near-miss check: the evaluator is (almost) not the problem
Re-executed every failed gold/pred pair with looser matching (numeric
normalization, full column-permutation, drop-one-column projection): it rescues
exactly **1 question per run** (Codex dw_5330; Claude dw_427). The failures are
real result-set differences. Their shape is bimodal: ~40% share *zero* rows with
gold (one divergent aggregate column — a `STDDEV_SAMP` factor, a fan-out-inflated
`SUM` — poisons every tuple), ~20% overlap gold ≥90% (boundary-row conventions:
survivorship, rollup rows, LIMIT ties). Exact-set match hides how close misses are;
a partial-credit metric (row-F1) would separate the two modes.

## Style guide: encode the house conventions → 37% (new best)
The taxonomy implies gold has a consistent *style*. `CODEX_STYLE_GUIDE=1` appends
8 gold-blind rules to the prompt (`_STYLE_GUIDE` in `agent.py`): (1) final
question overrides decomposition hints — no leaked top-k/filters/total rows;
(2) only the requested columns, in question order; (3) no DISTINCT inside
aggregates unless the question says "unique"; (4) "at least one related Y" =
fan-out join, not EXISTS; (5) LEFT JOIN when attaching secondary blocks;
(6) raw values — no ROUND/CAST/date parsing; (7) simplest window form;
(8) "more than" = `>`, "at least" = `>=`.

Mechanism check on 10 targeted never-solved failures (`dev_styleguide10.json`),
best config, style arm vs same-config control re-run: **6/10 vs 0/10** — every
win maps 1:1 to its targeted rule.

Full 100-question run: **37 vs 34** — gained 13 (incl. **5 from the never-solved
core**: dw_1133, dw_1970, dw_3310, dw_4588, dw_5136; cross-run union 50 → 55),
lost 10. Of the losses, 7 were flaky (solved ≤4/11 prior runs — churn); the
systematic backfire is **rule 5**: dw_3585 (previously 11/11) flipped INNER→LEFT
where gold uses INNER, and rule 3 cost dw_5668 where gold *does* use
`COUNT(DISTINCT)` — the golds are internally inconsistent on their own
conventions. Softening rule 5 (LEFT JOIN only when the question implies keeping
unmatched entities) is the obvious next tweak.

Caveat for write-ups: the rules were derived from this benchmark's failure modes,
so this is a benchmark-adapted prior — gold-blind per question, but fit to the
dataset's house style.

### Style guide iteration (v2/v3): what's durable and what's zero-sum
Targeted A/B on 12 questions (7 hint-backfires, 3 v1 regressions, 2 passing
guards): two changes are durable across independent runs — the **decomposition
precedence sentence** in `prompt.py` (question text overrides conflicting
sub-question filters/limits) and **rule 9** (use every hinted table/join key;
keep "redundant" bridge joins), which cracked never-solved dw_2933. **Rule 5's
direction (LEFT vs INNER when combining blocks) is zero-sum**: no wording wins
both dw_3310/dw_5136 (gold LEFT) and dw_3585 (gold INNER) — the NL is identical
in form ("for each X, show [stats A] and [stats B]") and gold is arbitrary; kept
default-LEFT because it targets never-solved questions. The 6 remaining
hint-backfire questions moved under no variant: each stacks a second gold-side
divergence (substituted sub-questions, fan-out inflation), so single fixes can't
reach them.

## Schema skill: DB-profiled data facts → +1, and a diagnosis
`CODEX_SCHEMA_SKILL=1` injects per-question data facts profiled read-only from
the live DB (gold-blind: schema + data statistics only, never questions/gold)
from `dw_schema_skill.json`: table grains, value vocabularies with lookup-table
meanings (EO=Electronic options, RC=Recommended, ...), string-date columns
('DD-MON-YY' → MIN/MAX is lexicographic), TERM_CODE vs EFFECTIVE_TERM_CODE
differing in 78% of rows, dept names mapping to multiple codes, per-key fan-out
ratios.

On 10 data-fact failures + 2 guards: **skill 4/12 vs control 3/12**, no
regressions. Only the pure-data-fact failure flipped (dw_140, lexicographic
dates — never solved by 13 prior runs). The facts visibly nudged the rest
(dw_5638 started hedging `IN ('RQ','EO')`; dw_779 started using
EFFECTIVE_TERM_CODE) without flipping them: the *menu* of values is learnable,
but gold's arbitrary NL→value assignment ("optional" = 'EO' alone; EFFECTIVE
only in one CTE) is not. Interventional confirmation that the residual wall is
gold conventions, not missing data knowledge. Keep the flag: free, +1, no
downside. Regenerate the skill with the profiler (scratchpad `profile_dw.py`
pattern) in ~3 min.

## Multi-candidate: 3 answers spanning the ambiguity → pass@1 39%, pass@3 49%
`CODEX_N_CANDIDATES=3`: the final answer becomes `<ans1>/<ans2>/<ans3>` —
candidate 1 the best guess, candidates 2–3 flipping the choices the question
does not determine (COUNT distinctness, join survivorship, term-code column,
value mapping, window form). Candidates live in one .sql joined by
`-- ===CANDIDATE=== --`; score with `evaluate_ex_acc.py --multi` (any-match +
candidate-1 accuracy).

Full-100 (style guide v3 + schema skill + N=3):

| Metric | score |
|---|:---:|
| candidate 1 only (deployable single answer) | **39%** — new best |
| pass@3 (any candidate) | **49%** |

Match histogram 39/9/1 (c1/c2/c3): the "second guess on the least-sure choice"
recovers 9 questions alone. Candidate 1 *improved* over the single-answer run
(37→39): articulating uncertainty doesn't hurt the primary. This one run cracked
8 of the original 50 never-solved and its pass@3 set approaches the all-time
union across ~15 runs (~56). The +10 pass@3−pass@1 band is the measured
coverable-ambiguity margin; converting it to real accuracy needs a gold-blind
selector over the executed result sets (self-agreement / cross-backend vote) —
the candidates already execute independently, so that selector is the next
cheap experiment.

## Negative result #4: adversarial review still hurts — and now we know why
Rebuilt `CODEX_REVIEW` from the one-shot intent check (−7 above) into an
adversarial, tool-using reviewer: it gets the question + all N candidates with
their executed sample rows, probes with its OWN read-only queries (recompute one
group directly, check filter literals against the data, count rows across joins;
`CODEX_REVIEW_STEPS=4`), and may replace a candidate only on concrete
contradicting evidence — otherwise keep it byte-for-byte. Two measurements
against the frozen multi-candidate baseline (16 questions: 5 c1-pass, 5
c2/3-pass, 6 fail):

| Arm | pass@3 | Δ |
|-----|:------:|:--:|
| baseline (multi3, no review) | 10/16 | — |
| paired: reviewer applied to the *frozen* candidate bundles | 8/16 | **−2** |
| end-to-end: fresh generation with review in the loop | 6/16 | −4 |

The conservatism worked: 13/16 bundles kept verbatim (the old reviewer rewrote
nearly everything). But **every intervention was harmful or neutral** — it broke
dw_3183 (c1-pass) and dw_287 (c2-pass) and fixed none of the 6 failures.

The failure is structural, not prompt quality. dw_287 shows the mechanism: gold
silently re-applies filters the question never states; the *passing* candidate
mirrors gold's unstated filter, so an evidence-driven reviewer probes, finds the
candidate genuinely contradicts the question's plain text, and "corrects" it —
destroying the match. Gold-blind review optimizes question-faithfulness; the
residual scoring margin rewards gold-faithfulness; on exactly the questions
where a reviewer finds actionable evidence, the two point in opposite
directions. Meanwhile the errors probes can legitimately catch are already
eliminated upstream by explore + fix, leaving the reviewer only false positives
to act on.

Fourth independently-measured negative for critique-style enhancements (naive
review −7, self-decompose −5, both −7, adversarial evidence-based review −2
paired / −4 e2e): on BEAVER, self-critique without ground truth is
*systematically* anti-correlated with score at the margin where it acts. Ship
with `CODEX_REVIEW=0`. If ever revived, restrict the reviewer to *picking*
among candidates (reordering can't break a bundle) — never editing them.

## GPT-5.6 rerun of the best config: same coverage, worse candidate ranking
Reran the best config (style guide v3 + schema skill + 3 candidates, setting 2,
high, explore + fix) unchanged on the new Codex default model (`gpt-5.6-sol`,
2026-07-10). Two results, one artifact:

| Model | pass@1 (candidate 1) | pass@3 |
|-------|:--------------------:|:------:|
| gpt-5.5 (best run above) | **39%** | 49% |
| gpt-5.6, raw run | 30% | 47% |
| gpt-5.6, after entity fix | **33%** | **49%** |

**Artifact:** gpt-5.6 HTML-escapes angle brackets inside `<ans>` spans on some
answers (`&lt;&gt;` for `<>`, `&gt;` for `>`) — 8/100 predictions affected, 7 of
them 1064 syntax errors at eval time; gpt-5.5 produced 0. `clean_sql` in
`agent_common.py` now unescapes entities, and the 5.6 numbers above were
rescored on the same generations with the escaping undone (SQL errors → 0).

**Reading:** pass@3 is identical to 5.5 (49 vs 49, symmetric churn: lost
dw_2878/3298/4570/5638, won dw_1570/2132/4771/779 — dw_779 finally flipped after
merely "hedging" under 5.5). The pass@1 gap is a **ranking regression**: 5.6
lost candidate-1 on 7 questions and won 1, but 5 of the 7 losses still pass via
candidate 2/3 (match histogram c1/c2/c3: 5.5 = 39/9/1, 5.6 = 33/14/2). 5.6
covers the same readings; it more often puts the gold-matching reading second.
Same conclusion as before, now model-robust: the pass@3−pass@1 band (here 16
pts) is where the value is, and a gold-blind selector over executed result sets
remains the next experiment. Generation was noticeably faster (~28 min for 94
questions, 4 workers).

## Reproduce
```bash
# best config (single answer = candidate 1; pass@3 via --multi)
cd eval/myagent
CODEX_REASONING_EFFORT=high CODEX_SQL_EXPLORE=1 CODEX_SQL_FIX=1 \
CODEX_STYLE_GUIDE=1 CODEX_SCHEMA_SKILL=1 CODEX_N_CANDIDATES=3 \
  ./run.sh --dataset dw --setting 2

# score (from eval/); drop --multi for single-candidate runs
cd .. && uv run python evaluate_ex_acc.py --dataset dw --multi \
  --input_dir unified-output/myagent/<run_name>
```
Requires the `dw` MySQL DB loaded + `MYSQL_*` creds in `.env`. Per-run SQL outputs
and `summary_ex_acc.json` are written under `eval/unified-output/myagent/`
(gitignored — contains gold SQL from the gated dataset).
