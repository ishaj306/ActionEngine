# The benchmark

    python eval/score.py              # the table
    python eval/score.py --detail     # every miss and every false positive

17 documents, each labelled with what a careful reader would extract. Run from
the repository root, with the API package installed (`pip install -e
services/api`).

## Why this exists

Before it, the project had 317 passing tests and no idea how well it worked.
Tests assert the behaviour we chose; this measures the behaviour we got. The
two fail differently, and only one of them can tell you that a confidence of
0.97 is meaningful.

## Adding a document

Drop `something.txt` in `corpus/` and `something.labels.json` beside it.

```json
{
  "document_type": "notice",
  "tags": ["gaps"],
  "deadlines": [
    {"date": "2026-09-18", "quote": "18 September 2026", "is_deadline": true}
  ],
  "actions": [
    {"verb": "obtain", "quote": "should obtain the income certificate",
     "gist": "obtain the certificate", "conditional": false, "optional": false}
  ],
  "requirements": [{"text": "income certificate", "kind": "document"}],
  "conditions": [{"attribute": "year", "comparator": "equals", "value": "3"}],
  "gaps": ["designated office"],
  "notes": "why this document is in the corpus"
}
```

**Labels cite quotes, not offsets.** The loader resolves each quote to a span by
searching the text, and refuses a quote that is missing or that appears twice --
extend it until it is unique. Hand-counting character offsets across a corpus
guarantees wrong ground truth, and a benchmark with wrong ground truth is worse
than none: it produces a plausible number nobody can audit.

That check earns its keep. It caught a label quoting `"the designated office"`
across a wrapped line, where the document actually contains `"to the\ndesignated
office"`.

`quote` is what the document says. `gist` is for humans reading scoring output
and is never matched on.

## What it measures

| Row | Matched on |
| --- | --- |
| dates (all) | Every date, cutoff or not. Separate from deadlines because they fail separately |
| deadlines (cutoffs) | Date value plus span overlap |
| actions (found) | Span overlap only -- did it notice the instruction |
| actions (verb correct) | Of those found, the right bucket |
| requirements | Content words, ≥60% overlap. Wording varies too much for string equality |
| eligibility conditions | Attribute, then comparator |
| information gaps | Overlap with the phrase that should raise the gap |
| conditional / optional | Whether modality is represented at all |
| calibration | Claimed confidence against observed accuracy |

Span matching is deliberately loose (30% of the shorter span). The engine
returns whole sentences and labels quote fragments; scoring boundary agreement
would measure punctuation rather than extraction.

**Calibration is the row that matters most.** Everything the engine emits
carries a confidence, and this is the only thing that says whether that number
means anything.

## Honest limitations

- **The documents are synthetic**, written to match the patterns of Indian
  college and government notices. Real scanned documents will be harder, and
  the numbers here are an upper bound until real ones are added.
- **17 is a seed, not a corpus.** Individual cells move by whole percentage
  points on one document. Treat the shape as informative and the third decimal
  as noise.
- **The author of the labels wrote the engine**, which is the standard way to
  produce a benchmark that flatters the system. The negative controls
  (`informational-history`, `job-description-backend`, `leave-policy`,
  `application-form`) exist as a partial defence: anything extracted from them
  is a false positive.
