"""Citation integrity.

1. verify_quote      - is an evidence quote really present in the fetched page text?
2. normalize_citations - every [n] in the report must map to a real source; invalid markers are
                         removed, citations are renumbered by first appearance, and the reference
                         list is built by code from collected sources (the LLM never writes it).
3. extract_cited_claims / review_citations - Claude checks each cited sentence against the exact
                         evidence collected from the cited sources.
"""
from __future__ import annotations

import re
import unicodedata

from ..llm import UNTRUSTED_NOTICE, ClaudeLLM
from ..schemas import CitationCheck, CitationReview, Evidence, Reference, Source

CITATION_RE = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\](?!\()")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[*])")
_TRANSLATE = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).lower()
    return re.sub(r"\s+", " ", text).strip()


def verify_quote(quote: str, page_text: str) -> bool:
    """True if every part of the quote (split on ellipses) appears verbatim in the page text."""
    haystack = _norm(page_text)
    parts = [p.strip(" .\"'") for p in re.split(r"\.{3,}|…|\[\.\.\.\]", _norm(quote))]
    parts = [p for p in parts if len(p) >= 12]
    return bool(parts) and all(p in haystack for p in parts)


def _numbers(match: re.Match) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", match.group(1))]


def normalize_citations(
    sections: dict[str, str], sources: list[Source]
) -> tuple[dict[str, str], list[Reference], dict]:
    """`sources` is the list the writer saw, numbered 1..N in order."""
    order: list[int] = []  # original numbers in order of first valid appearance
    invalid = 0
    for text in sections.values():
        for match in CITATION_RE.finditer(text):
            for n in _numbers(match):
                if 1 <= n <= len(sources):
                    if n not in order:
                        order.append(n)
                else:
                    invalid += 1
    renumber = {old: new for new, old in enumerate(order, start=1)}

    def replace(match: re.Match) -> str:
        valid = sorted({renumber[n] for n in _numbers(match) if n in renumber})
        return f"[{', '.join(map(str, valid))}]" if valid else ""

    cleaned = {key: re.sub(r" +([.,;:])", r"\1", CITATION_RE.sub(replace, text)) for key, text in sections.items()}
    references = [
        Reference(
            number=new,
            source_id=sources[old - 1].id,
            title=sources[old - 1].title,
            url=sources[old - 1].url,
            publisher=sources[old - 1].publisher,
            published_date=sources[old - 1].published_date,
            retrieved_date=sources[old - 1].retrieved_date,
        )
        for old, new in renumber.items()
    ]
    total = sum(len(_numbers(m)) for text in cleaned.values() for m in CITATION_RE.finditer(text))
    return cleaned, references, {"total_citations": total, "invalid_citations_removed": invalid}


def extract_cited_claims(sections: dict[str, str]) -> list[dict]:
    """Sentences that carry at least one citation: [{id, claim, citations}]."""
    claims: list[dict] = []
    for text in sections.values():
        for block in text.split("\n"):
            block = re.sub(r"^\s*(?:[-*+]|\d+\.|#+|>)\s+", "", block).strip()
            for sentence in _SENTENCE_SPLIT.split(block):
                numbers = sorted({n for m in CITATION_RE.finditer(sentence) for n in _numbers(m)})
                if numbers:
                    claims.append({"id": len(claims) + 1, "claim": sentence.strip(), "citations": numbers})
    return claims


REVIEW_SYSTEM = f"""You are a citation auditor. For each numbered claim from a research report you are given the \
evidence collected from the sources it cites. Decide whether that evidence supports the claim.

- supported: the evidence directly states or clearly implies the claim, including any numbers.
- partially_supported: the evidence supports part of the claim, or the claim overstates it.
- unsupported: the evidence does not back the claim, or contradicts it.

Judge only from the evidence shown - not from your own knowledge. Return one verdict per claim id with a \
one-sentence reason.

{UNTRUSTED_NOTICE}"""


async def review_citations(
    llm: ClaudeLLM, claims: list[dict], references: list[Reference], evidence: list[Evidence]
) -> list[CitationCheck]:
    if not claims:
        return []
    by_source: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_source.setdefault(item.source_id, []).append(item)
    ref_by_number = {r.number: r for r in references}

    blocks = []
    for claim in claims:
        lines = [f'<claim id="{claim["id"]}">', claim["claim"]]
        for number in claim["citations"]:
            ref = ref_by_number[number]
            lines.append(f'<evidence citation="{number}" source="{ref.title}">')
            items = by_source.get(ref.source_id, [])
            lines += [f'- {e.claim} | quote: "{e.quote}"' for e in items] or ["(no evidence was extracted from this source)"]
            lines.append("</evidence>")
        lines.append("</claim>")
        blocks.append("\n".join(lines))

    review = await llm.structured(
        agent="citation_validator", system=REVIEW_SYSTEM, user="\n\n".join(blocks), schema=CitationReview
    )
    verdicts = {v.claim_id: v for v in review.verdicts}
    return [
        CitationCheck(
            claim=c["claim"],
            citations=c["citations"],
            verdict=verdicts[c["id"]].verdict if c["id"] in verdicts else "not_checked",
            reason=verdicts[c["id"]].reason if c["id"] in verdicts else "",
        )
        for c in claims
    ]


def citation_stats(checks: list[CitationCheck]) -> dict:
    counts = {v: sum(1 for c in checks if c.verdict == v) for v in ("supported", "partially_supported", "unsupported", "not_checked")}
    checked = len(checks) - counts["not_checked"]
    return {
        "claims_checked": checked,
        **counts,
        # Share of checked claims fully supported by the evidence behind their citations.
        "citation_accuracy": round(counts["supported"] / checked, 3) if checked else None,
    }
