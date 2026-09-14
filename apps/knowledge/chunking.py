"""
Markdown parsing and chunking.

Chunks are split on `##` headings rather than at a fixed character count,
because the heading is what makes a citation useful: "per the Payment Status
Reference, section 'PENDING'" is verifiable, "per chunk 7" is not. Long
sections are then split on paragraph boundaries with a small overlap so a
sentence never lands mid-thought at a chunk edge.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
HEADING_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

MAX_CHUNK_CHARS = 1100
#: Floor for a *prefixed* chunk (heading included). Deliberately low: it is
#: meant to drop stray fragments like a lone "---", not real content. A
#: glossary entry such as "## CANCELLED — No money moved." is short and
#: genuinely useful, and an earlier 80-character floor silently discarded
#: exactly that kind of section.
MIN_CHUNK_CHARS = 25
OVERLAP_CHARS = 120


@dataclass
class ParsedDocument:
    metadata: dict
    body: str
    checksum: str


@dataclass
class ParsedChunk:
    sequence: int
    heading: str
    content: str


def parse_document(text: str) -> ParsedDocument:
    """Split simple `---` frontmatter from the markdown body."""
    metadata: dict = {}
    body = text

    match = FRONTMATTER_PATTERN.match(text)
    if match:
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            metadata[key.strip()] = value.strip()
        body = text[match.end() :]

    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ParsedDocument(metadata=metadata, body=body.strip(), checksum=checksum)


def chunk_markdown(body: str, *, document_title: str = "") -> list[ParsedChunk]:
    """Split a markdown body into heading-scoped, size-bounded chunks."""
    sections = _split_sections(body)

    chunks: list[ParsedChunk] = []
    sequence = 1
    for heading, content in sections:
        for piece in _split_long(content):
            # Prefixing the heading means the embedding carries the section
            # topic even when the paragraph itself never names it — a chunk
            # under "PENDING" that only says "do not ask the customer to pay
            # again" is otherwise unfindable by a query about PENDING.
            prefix = f"{document_title} — {heading}\n\n" if heading else ""
            text = (prefix + piece.strip()).strip()
            # Measured on the finished chunk, not the raw paragraph, so a
            # short section still carries its heading into the index.
            if len(text) < MIN_CHUNK_CHARS:
                continue
            chunks.append(
                ParsedChunk(sequence=sequence, heading=heading, content=text)
            )
            sequence += 1
    return chunks


def _split_sections(body: str) -> list[tuple[str, str]]:
    matches = list(HEADING_PATTERN.finditer(body))
    if not matches:
        return [("", body)]

    sections: list[tuple[str, str]] = []

    preamble = body[: matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((match.group(1).strip(), body[start:end].strip()))

    return sections


def _split_long(content: str) -> list[str]:
    if len(content) <= MAX_CHUNK_CHARS:
        return [content]

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content) if p.strip()]
    pieces: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= MAX_CHUNK_CHARS:
            current = candidate
            continue
        if current:
            pieces.append(current)
            # Carry a tail of the previous piece so a rule split across two
            # chunks is still retrievable from either side.
            current = (current[-OVERLAP_CHARS:] + "\n\n" + paragraph).strip()
        else:
            current = paragraph

    if current:
        pieces.append(current)
    return pieces
