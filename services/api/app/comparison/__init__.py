"""Reasoning over finished analyses rather than over documents.

Everything under `modules/` reads one document. These two read the *output* of
that work: `changes` compares two readings of the same document across
versions, and `crossdoc` holds several documents' findings side by side.

That is why they live above `pipeline` instead of inside `modules/`. Their
input is an `Analysis`, so importing one from inside `modules/` would mean a
module depending on the composer that assembles it.
"""
