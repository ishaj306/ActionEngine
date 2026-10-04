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

Five behaviours, none of which the extraction layer alone gives you.

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

**Uncertainty compounds honestly.** Certainty about a sentence cannot exceed
certainty about its characters. When a page is read by OCR at 61% mean
confidence, every claim drawn from it is capped there — a deadline that would
be a 97% `FACT` in a native PDF comes back as an `INFERENCE`, and says why.

**It reads a reissue as a diff of findings, not of text.** A revised notice is
retyped, reflowed and renumbered, so a line-by-line diff is almost all noise
and the four-day deadline move is one changed line among two hundred. Comparing
the *findings* — the deadline, the steps, the requirements, the eligibility
conditions — produces three lines of signal:

```
The deadline moved forward by 4 days, from 18 Sep to 14 Sep.
   2026-09-18 → 2026-09-14
New eligibility condition: at least 70% aggregate. Check that you still qualify.
You now also need: Caste certificate.
```

---

## Does it apply to you?

A notice restricts itself in one sentence and never mentions it again. The
reader who fails that restriction builds the whole plan and finds out at the
counter.

The form asks only for the attributes *this* document restricts — derived from
the conditions found in its text, because a seven-field profile in front of a
notice that mentions one condition is a tax on the reader. Two rules keep the
answer honest:

**A conflict is only asserted where the comparison is arithmetic.** Year, marks
and age are numbers, and a number either clears a bar or does not. Programme
and place are open vocabularies: a profile saying `CSE` against a document
saying `Computer Science` shares no token, and reporting that as *you are not
eligible* would be a confident, wrong and consequential answer. Those come back
open, with both strings shown.

**A criterion that cannot be turned into a comparison says so.** "Final year"
is a real restriction whose number depends on the length of the programme. It
is extracted, displayed, and marked as something the engine declines to decide.

The verdict annotates the plan and never hides it. The extraction is lexical
and can miss, and a plan withheld on a false negative is a worse failure than a
plan shown under a warning. A document that states *no* condition produces a
`MISSING` claim: not saying who it is for is a finding about the document.

---

## Taking it out of the app

The calendar file puts each step on the day work has to **start**, not the day
it is due — putting the deadline in the calendar instead is how people start a
ten-day errand two days out. Each event carries its source sentence and its
confidence, and an entry below the actionable threshold is written `TENTATIVE`
rather than `CONFIRMED`. Steps with no governing date get no event, because
guessing a date into somebody's calendar is worse than leaving it to them.

Every gap the engine reports is, from the reader's side, an email they have to
write — so it writes it, quoting the phrase that raised each question so the
person answering can find the sentence and reply once.

Both are a file and a draft rather than live integrations. OAuth scopes, a
token store and a consent screen would buy what a download already does, and
would write into somebody's real calendar on the strength of an extraction.

---

## What is deterministic and what is not

Dates are arithmetic, so no model touches them. A model reading "18 September"
guesses the year; a model reading `03/04/2026` guesses the locale. Both guesses
are invisible in the output and wrong about half the time.

| Stage | Implementation | Why |
| --- | --- | --- |
| Ingestion, page mapping | Deterministic | Offsets must be exact or every citation is wrong |
| OCR | Swappable engine | Local for privacy, cloud for accuracy; must refuse when it cannot run |
| Date resolution | Deterministic | One correct answer; must be reproducible |
| Classification | Weighted lexical | Cheap, inspectable, and its margin *is* its confidence |
| Dependency graph, scheduling | Deterministic | Topological order and slack are arithmetic |
| Evidence anchoring | Fuzzy match, scored | Models quote approximately; the score travels with the span |
| Action & requirement extraction | Hybrid, rules first | The rule arm is the evaluation baseline and the fallback, not a stub |
| Eligibility matching | Deterministic | Comparing a self-declared number to a stated bar is arithmetic |
| Eligibility *extraction* from prose | Rules today, model next | Restrictions are phrased a hundred ways; the rules cover the common ones |
| Model output | Verified against the source | Every quote must anchor, or the claim is discarded |
| Revision and cross-document diff | Deterministic | Set comparison over findings that already carry their own confidence |
| Ambiguity, cross-sentence attachment | Model | No rule-based answer exists |

`03/04/2026` comes back flagged with **both** readings and a confidence of 0.45,
not silently disambiguated.

---

## OCR

`modules/ingestion/ocr.py` defines an `OcrEngine` protocol with a Tesseract
implementation and a null engine. Availability is checked against the binary on
`PATH`, not the Python wrapper, because `pytesseract` imports cleanly without
Tesseract installed and fails only at call time.

An unavailable engine raises. It never returns empty text — that would present
a scan the system could not read as a document containing nothing, which is the
exact failure everything else here is built to prevent. Scanned PDF pages are
read from the images already embedded in the file rather than adding a second
PDF library purely to redraw what is already there.

Install Tesseract to enable it; without it, images and scans are refused with a
reason rather than silently producing an empty plan.

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

## How well it works

```bash
python eval/score.py
```

17 labelled documents. Every quote in the labels is resolved against the source
and refuses to load if it is absent or ambiguous.

| Stage | Gold | P | R | F1 |
| --- | ---: | ---: | ---: | ---: |
| Dates (all) | 17 | 0.94 | 0.94 | 0.94 |
| Deadlines (cutoffs) | 12 | 1.00 | 1.00 | 1.00 |
| Actions found | 22 | 1.00 | 1.00 | 1.00 |
| Action verb correct | 22 | 1.00 | 1.00 | 1.00 |
| Requirements | 21 | 1.00 | 1.00 | 1.00 |
| Requirement kind | 21 | 1.00 | 1.00 | 1.00 |
| Eligibility conditions | 6 | 1.00 | 1.00 | 1.00 |
| Information gaps | 8 | 1.00 | 1.00 | 1.00 |
| Conditional / optional | 4 | 1.00 | 1.00 | 1.00 |
| Document type | 17 | — | — | 0.88 accuracy |

**Read this table as "no known failure remains in this corpus", not as "the
engine is accurate."** Those are different claims and only the first is
supported. The corpus is synthetic, it has 17 documents, and the same person
wrote the labels and the code — which is the standard recipe for a benchmark
that flatters its system. Four negative controls are a partial defence:
anything extracted from them is a false positive. Real scanned documents will
be harder, and the honest next step is to add them and watch these numbers
fall.

The calibration table is the one that matters and the one currently least
trustworthy. Claims asserted at 0.95 are right 100% of the time here, which
says the corpus is too easy rather than that the confidence is well calibrated.
`0.97` remains a constant chosen by hand, not a measured probability.

Where it started, before any of Phase 1's fixes:

| Stage | Before | After |
| --- | ---: | ---: |
| Deadlines | 0.82 | 1.00 |
| Actions | 0.89 | 1.00 |
| Requirements | 0.55 | 1.00 |
| Conditional / optional | 0.00 | 1.00 |

---

## Accounts and isolation

Sign-in is Clerk, restricted to Google in the Clerk dashboard. The API verifies
each session token against Clerk's published keys and takes the user id from the
token — never from a header or a request body, because a client-supplied
identity is not an identity.

**With no issuer configured, every document route returns 503.** That costs a
little friction locally and buys the guarantee worth having: this cannot be
deployed unauthenticated by forgetting to set something. `AUTH_DEV_USER` is the
local escape hatch; it must be set deliberately and logs a warning on every
request that uses it.

Ownership is enforced inside the store rather than at each call site, so a route
that forgets to filter cannot be written. Another user's document is a **404,
not a 403** — a 403 confirms the id exists, which is what a probe is looking
for. Document ids are random rather than content hashes: a hash is identical for
everyone who uploads the same public circular, which turns an id into a guess
anyone can make.

An integration test drives every route as a second user and requires a 404 from
each. Its failure is a disclosure, not a bug report.

Per-account rate limits are in-process, so two replicas each allow the full
quota. That is a real limitation, stated in the module rather than hidden; the
fix is a shared counter, and it belongs with the deployment work rather than
ahead of it.

---

## The model arm

Off by default. `EXTRACTION_MODE=hybrid` plus a provider key turns it on;
without both, the engine runs the rules alone and behaves exactly as it did
before the arm existed. That is not timidity — the rule arm is a complete
system, which is what makes it a fair baseline.

The provider sits behind one `ModelExtractor` interface, so the arm is not tied
to a vendor. Two backends ship: **Gemini** (`GEMINI_API_KEY`, the default) and
**Anthropic** (`ANTHROPIC_API_KEY`). `MODEL_PROVIDER` chooses, or it is inferred
from whichever key is present. Both read the same prompt and return the same
schema, so switching providers is a config change, not a code change — and the
verification below protects either one identically, because it works on the
output, not the model.

Three structural choices make invention hard rather than merely detectable.

**The model cannot emit a date.** Dates are arithmetic and already resolved, so
the model receives the dates that were found, each with an id, and may only
*reference* one. There is no field it could write `18 September 2026` into. A
hallucinated deadline is not caught — it is unrepresentable. An id that is not
on the list resolves to nothing, and the action loses its deadline rather than
acquiring an invented one.

**Every claim must quote the document verbatim,** and `anchor()` has to resolve
that quote to real offsets in the real text:

```
quote → anchor(quote, document)
        ├── None ............... DROP. The document does not say this.
        ├── below the floor .... KEEP, demoted to UNCERTAIN.
        └── resolved ........... KEEP, with real offsets.
```

The hardest module in v1 turns out to be the hallucination detector. A model
that invents a sentence produces a quote no fuzzy matching will find, so the
finding is discarded before it can become a `Claim` — which would refuse it
anyway, since a FACT without evidence raises at construction.

The middle branch matters too. Models quote approximately: a dropped article,
re-flowed whitespace. That is not fabrication and dropping it throws away good
findings, but it is not verbatim either, and the difference belongs in the
confidence rather than in a footnote.

**The model has no tools,** and the document arrives inside delimiters in a user
message, never in the system prompt. The structural defence is the one that
counts: an output schema of actions and requirements has no field an injected
instruction could occupy. A document reading `IGNORE ALL PREVIOUS INSTRUCTIONS`
can at worst become a row in a checklist.

Where both arms find the same instruction the rule reading wins — not because
it is better written, but because it is reproducible, which is what makes the
benchmark mean anything. The model's contribution is what the rules never saw.

A model failure degrades to the rules rather than to an error page. A notice
the reader needs today beats a perfect reading of it.

**The ablation is not yet run.** `eval/score.py --arm hybrid` refuses rather
than silently reporting rule-arm numbers under a hybrid heading, and it records
every response so a run costs money once. The table below has one row until
someone spends the money to fill in the other.

---

## Running it in anger

```bash
docker compose up --build
```

Brings up the API against a real Postgres. Worth doing before a deploy for one
reason: the suite proves the store works against SQLite, which is the same
SQLAlchemy code path but not the same database, and this is where a
Postgres-specific difference surfaces while it is still cheap to find.

The container runs as a non-root user and ships Tesseract, so scans are read
rather than refused.

**Logging is JSON, and carries identifiers, counts and timings only — never
document text.** Not an extracted deadline, not a requirement, not a sentence.
A log line is the easiest place in a system for content to reach an aggregator
or an error tracker nobody audited, so the rule is absolute rather than
case-by-case. `GET /v1/usage` reports model spend for the process; watch
`cache_hit_rate`, because a rate stuck near zero means something volatile has
leaked into the cached prefix and every call is paying full price for it.

`/privacy` says all of this to the reader in their own terms, and is excluded
from the auth middleware entirely — the one page someone must be able to open
*before* deciding to trust this system should not depend on a third party being
reachable.

---

## Storage

`DATABASE_URL` chooses: unset gives an in-process map, a URL gives Postgres.
Both implementations satisfy one protocol and are run through the same contract
suite, because a persistence layer tested only in the configuration nobody
deploys is not tested. CI runs the whole API suite twice, once against each.

**What is stored is the document, not the analysis.** A row holds the text, the
page map and the reader's own state; the plan is re-derived on read in about
five milliseconds. Caching the analysis would be faster and would be the wrong
trade — the analysis shape changes with every extraction improvement, and a
column holding last month's output is worse than no column because it looks
current. The document and what the reader ticked off are the only durable facts
here.

The uploaded file itself is not kept. Once the text is out, holding somebody's
scanned identity document on disk buys nothing and adds a category of breach.

Two gaps worth naming. There are no migrations: `create_all` bootstraps the
schema, which is honest for a system with no deployment and no data, and the
first schema change after either exists needs Alembic. And SQLite stands in for
Postgres in the tests — the same SQLAlchemy code path and the same SQL, but not
Postgres behaviour under concurrency.

---

## Running it

Python 3.11 or newer.

```bash
cd services/api && pip install -e ".[dev]" && python -m uvicorn app.main:app --port 8000
```

```bash
cd apps/web && npm install && npm run dev
```

The `ocr` extra (`pip install -e ".[dev,ocr]"`) adds the Python side of OCR, and
the `tesseract` binary has to be on `PATH` for it to do anything. Without either,
scans are refused with a reason rather than read as empty documents, and every
test still passes — CI has a job that installs without the extra to keep that
true.

Copy `.env.example` to `.env` if you need to change the ports or origins.

Open <http://localhost:3000>. Two sample documents are built in; neither is
chosen to flatter the engine — the first hides a prerequisite that no longer
fits its deadline, the second is vague in three places.

```bash
cd services/api && python -m pytest -q     # 317 tests
```

### API

| | |
| --- | --- |
| `POST /v1/documents` | Upload a PDF, image or text file |
| `POST /v1/documents/text` | Analyse pasted text |
| `PATCH /v1/documents/{id}` | Set completed steps or a profile; returns the re-derived plan |
| `GET /v1/documents/{id}/changes?since=` | Diff two readings of the same document |
| `POST /v1/portfolio` | Read several documents together; reports contradictions |
| `GET /v1/documents/{id}/calendar.ics` | The dated steps, on their start dates |
| `GET /v1/documents/{id}/enquiry` | A draft email asking what the document omits |

---

## Layout

```
services/api/app/
  domain/           claims, spans — the epistemic types everything else returns
  modules/
    ingestion/      parse → text + page map; OCR behind an engine protocol
    evidence/       offset-preserving normalization, fuzzy span anchoring
    extraction/     temporal, classification, requirements, rules (baseline)
    action_engine/  dependency graph, backward scheduling, priority
    reasoning/      eligibility conditions, checked against a declared profile
  pipeline.py       composes the stages into one Analysis
  comparison/       revision diff and cross-document contradiction — these read
                    finished analyses, so they sit above the pipeline, not in it
  api/              wire format, .ics and email export; the serialization invariant

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

**Every planned feature is built.** PDF, image and text ingestion with OCR;
document classification; deadline, requirement, action and gap extraction; the
four-state epistemic model; per-claim evidence anchoring; dependency-aware
backward scheduling and priority; eligibility matching against a self-declared
profile; calendar and email export; server-side completion tracking; revision
comparison; and cross-document contradiction detection. 337 tests, and a
17-document benchmark that measures them.

Four limitations worth naming, all deliberate:

- **A deadline attaches to an action only when it appears in that action's own
  sentence.** Linking a date across sentences is exactly the confident guess
  this engine avoids by rule, and is the model stage's job.
- **Eligibility conflicts are never asserted on free text.** A programme or a
  place that does not match comes back open rather than disqualifying.
- **No dependency is inferred across documents.** Whether the circular's step
  blocks the notice's step is a question about the world, not the text, so the
  merged timeline orders by date and attributes every step to its source.
- **An action with no evidence of a prerequisite shows no dependency.** A
  verb-order fallback used to invent one, and the invented edge fed backward
  propagation, so a fabricated ordering produced a fabricated start date
  carrying the same confidence as a real one.

The verification pass exists and is wired: a date claim whose cited span does
not state it is demoted to `UNCERTAIN`. Against the rule arm it never fires,
because a rule's span is the text it matched. It is built for the model arm,
whose spans will not have that property.

Next, in order: authentication and tenant isolation, which the API has none of;
then the model extraction arm behind the same `Claim` contract, scored against
this benchmark as a rules-vs-model-vs-hybrid ablation; then Postgres behind the
existing `Store` boundary.

The design rationale, competitive analysis and full roadmap live in a separate
strategy document, published rather than checked in.
