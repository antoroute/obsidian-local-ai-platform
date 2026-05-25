from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


@dataclass(frozen=True)
class MarkdownChunk:
    chunk_index: int
    heading_path: str | None
    content: str
    content_hash: str
    token_estimate: int


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def split_markdown_chunks(markdown: str, *, chunk_size: int, chunk_overlap: int) -> list[MarkdownChunk]:
    body = strip_frontmatter(markdown).strip()
    if not body:
        return []

    sections = collect_heading_sections(body)
    raw_chunks: list[tuple[str | None, str]] = []
    for heading_path, section_text in sections:
        for piece in split_text_preserving_paragraphs(section_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap):
            if piece.strip():
                raw_chunks.append((heading_path, piece.strip()))

    return [
        MarkdownChunk(
            chunk_index=index,
            heading_path=heading_path,
            content=content,
            content_hash=sha256_text(content),
            token_estimate=max(1, len(content) // 4),
        )
        for index, (heading_path, content) in enumerate(raw_chunks)
    ]


def strip_frontmatter(markdown: str) -> str:
    return FRONTMATTER_RE.sub("", markdown, count=1)


def collect_heading_sections(markdown: str) -> list[tuple[str | None, str]]:
    heading_stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_heading: str | None = None
    sections: list[tuple[str | None, str]] = []

    for line in markdown.splitlines():
        match = HEADING_RE.match(line)
        if match:
            if current_lines:
                sections.append((current_heading, "\n".join(current_lines).strip()))
                current_lines = []
            level = len(match.group(1))
            title = match.group(2).strip()
            heading_stack = [(existing_level, existing_title) for existing_level, existing_title in heading_stack if existing_level < level]
            heading_stack.append((level, title))
            current_heading = " > ".join(title for _, title in heading_stack)
        current_lines.append(line)

    if current_lines:
        sections.append((current_heading, "\n".join(current_lines).strip()))

    return sections or [(None, markdown)]


def split_text_preserving_paragraphs(text: str, *, chunk_size: int, chunk_overlap: int) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > chunk_size:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(split_long_text(paragraph, chunk_size=chunk_size, chunk_overlap=chunk_overlap))
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            overlap = current[-chunk_overlap:].strip() if chunk_overlap > 0 else ""
            current = f"{overlap}\n\n{paragraph}".strip() if overlap else paragraph
    if current:
        chunks.append(current.strip())
    return chunks


def split_long_text(text: str, *, chunk_size: int, chunk_overlap: int) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(end - chunk_overlap, start + 1)
    return [chunk for chunk in chunks if chunk]
