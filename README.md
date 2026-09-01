# Document → Action Engine

Turns a notice into an evidence-backed, dependency-ordered plan — and says
plainly what the document never states.

Most document AI answers *what does this say*. This answers *what do I have to
do, in what order, starting when* — and refuses to assert anything it cannot
trace back to a sentence in the source.

```
Upload a scholarship notice
  ↓
Deadline  18 Sep 2026            Stated · 97%   ← click to see the sentence
  1. Verify your eligibility      start by  9 Sep
  2. Obtain the income certificate start by 10 Sep   ← due 17 Sep, inherited
  3. Submit the application        start by 17 Sep

The document does not say
  · Which office is this?     "the designated office"
  · How much is the fee?      "a nominal fee"
```

---

## Why this is not another PDF summarizer

Three behaviours, none of which the extraction layer alone gives you.

**Every claim carries its epistemic status.** Nothing reaches the interface as
bare text. A value is `FACT`, `INFERENCE`, `UNCERTAIN` or `MISSING`, with a
confidence and the reason for it. The invariant is enforced in the type system:
a `FACT` or `INFERENCE` without a cited span raises at construction, so an
unsourced fact cannot physically exist in the running program.

```python
class Claim(BaseModel, Generic[T]):
    value: T
    classification: ClaimClass
    confidence: Confidence
    evidence: EvidenceSpan | None   # required for FACT and INFERENCE
```

**Deadlines propagate backwards through the dependency graph.** A certificate
that takes ten days, needed for a deadline eight days out, is already late. The
scheduler computes a *latest safe start* per step by inheriting the tightest
downstream constraint, so slack can go negative and the plan reports itself
infeasible. In the sample above, "obtain the income certificate" states no
deadline anywhere in the document — it inherits 17 September from the
submission that depends on it.

**Gaps are a first-class output.** "Submit to the designated office" is not an
answer, and the engine says so rather than quietly dropping it. Detection is
deliberately conservative: a false gap teaches the reader to ignore the panel.

---

## What is deterministic and what is not

Dates are arithmetic, so no model touches them. A model reading "18 September"
guesses the year; a model reading `03/04/2026` guesses the locale. Both guesses
are invisible in the output and wrong about half the time.

| Stage | Implementation | Why |
| --- | --- | --- |
| Ingestion, page mapping | Deterministic | Offsets must be exact or every citation is wrong |
| Date resolution | Deterministic | One correct answer; must be reproducible |
| Dependency graph, scheduling | Deterministic | Topological order and slack are arithmetic |
| Evidence anchoring | Fuzzy match, scored | Models quote approximately; the score travels with the span |
| Action & requirement extraction | Rules today, hybrid next | The rule arm is the evaluation baseline, not a stub |
| Relevance, ambiguity | Model (next phase) | No rule-based answer exists |

`03/04/2026` comes back flagged with **both** readings and a confidence of 0.45,
not silently disambiguated.

---

## Evidence anchoring

The hardest correctness problem in the system. A model asked to cite its source
returns a near-verbatim quotation — usually right, often re-spaced, sometimes
missing an article. Trusting it verbatim means most citations fail to resolve;
trusting it loosely means a claim can be "sourced" to text that does not support
it.

`modules/evidence` normalizes both sides while recording, per character, which
source index produced it — so a match found in normalized space maps back to
exact offsets in the raw document. Matching is exact-first, then anchored on the
quote's *rarest* tokens rather than its first (quotes routinely open with a
stopword occurring thousands of times), then scored and tightened to the aligned
region.

It handles hyphenated line breaks, ligatures, typographic punctuation and
collapsed whitespace — and returns `None` for a quotation the document does not
contain, which is the case that makes the rest meaningful.

---

## Running it

```bash
cd services/api && python -m uvicorn app.main:app --port 8000
```

```bash
cd apps/web && npm install && npm run dev
```

Open <http://localhost:3000>. Two sample documents are built in; neither is
chosen to flatter the engine — the first hides a prerequisite that no longer
fits its deadline, the second is vague in three places.

```bash
cd services/api && python -m pytest -q     # 163 tests
```

---

## Layout

```
services/api/app/
  domain/           claims, spans — the epistemic types everything else returns
  modules/
    ingestion/      parse → text + page map
    evidence/       offset-preserving normalization, fuzzy span anchoring
    extraction/     temporal (deterministic), rules (baseline + fallback)
    action_engine/  dependency graph, backward scheduling, priority
  pipeline.py       composes the stages into one Analysis
  api/              wire format; enforces the serialization invariant

apps/web/
  components/       ClaimTag, PlanPane, SourcePane
  lib/api.ts        typed client mirroring the server's response models
```

A modular monolith, deliberately. Microservices at this size would be costume;
the module boundaries here are where services would later split.

**Interface.** Colour carries epistemic and urgency signal and nothing else —
the chrome is graphite, so a spot of colour is always worth looking at. Every
coloured state also carries a text label and a glyph whose fill level tracks
certainty, so meaning survives greyscale, colour blindness and a screen reader.

---

## Status

Working end to end: ingestion, temporal extraction, rule-based action and gap
extraction, dependency scheduling, evidence anchoring, API, and the workspace
interface. 163 tests.

Next, in order: the model extraction arm behind the same `Claim` contract with a
verification pass that demotes any claim its cited span does not entail; the
annotated benchmark and the rules-vs-model-vs-hybrid ablation; Postgres behind
the existing `Store` boundary; then document change detection.

Design rationale, competitive analysis and the full roadmap are in
[`strategy.html`](strategy.html).
