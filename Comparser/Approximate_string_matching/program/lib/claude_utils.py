from __future__ import annotations

import json
import os
import re
import sys
import textwrap
from typing import Dict, Sequence

try:
    import anthropic  # type: ignore
except ImportError:  # pragma: no cover
    anthropic = None

from .constants import CLAUDE_ALLOWED_DECISIONS, DEFAULT_CLAUDE_MODEL
from .model import WorkingRow

SYSTEM_PROMPT = textwrap.dedent(
    """\
    You review transcript rows that a local approximate string matching pipeline could not align confidently.
    Each row belongs to a spoken performance read from a written reference HTML.
    Choose exactly one decision per row:
    - ELIMINATE_OFF_TOPIC: spoken text is unrelated to the reference.
    - ELIMINATE_META_COMMENTARY: spoken text is a production note or commentary about recording/editing/script handling.
    - KEEP_TRANSCRIPTION_ERROR: spoken text likely belongs to the reference, but transcription is wrong.
    - KEEP_UNMATCHED: spoken text should stay, but the provided context is insufficient to align it safely.
    Return strict JSON only.
    """
)

PROMPT_TEMPLATE = textwrap.dedent(
    """\
    Return JSON with this shape:
    {{
      "rows": [
        {{"row_id": "12", "decision": "KEEP_UNMATCHED", "notes": "short reason"}}
      ]
    }}

    Allowed decisions: {allowed}

    Rows:
    {payload}
    """
)


def resolve_api_key(candidate: str | None = None) -> str:
    api_key = candidate or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Anthropic API key missing. Set ANTHROPIC_API_KEY before running stage 06.")
    return api_key


def extract_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []):
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _close_json_brackets(raw: str) -> str:
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in raw:
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_string:
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char in "{[":
            stack.append(char)
        elif char == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif char == "]" and stack and stack[-1] == "[":
            stack.pop()
    closing = "".join("}" if opener == "{" else "]" for opener in reversed(stack))
    return raw + closing


def parse_review_response(raw: str) -> Dict[str, dict[str, str]]:
    content = raw.strip()
    if not content:
        print("WARNING: Claude returned an empty review response; defaulting unmatched rows to KEEP_UNMATCHED.", file=sys.stderr)
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", content, re.DOTALL)
    if fenced:
        content = fenced.group(1).strip()
    elif "{" in content and "}" in content:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and start < end:
            content = content[start : end + 1].strip()
    try:
        payload = json.loads(_close_json_brackets(content))
    except json.JSONDecodeError:
        print(
            "WARNING: Claude review response was not valid JSON; defaulting unmatched rows to KEEP_UNMATCHED.",
            file=sys.stderr,
        )
        return {}
    result: Dict[str, dict[str, str]] = {}
    for item in payload.get("rows", []):
        row_id = str(item.get("row_id", "")).strip()
        decision = str(item.get("decision", "")).strip()
        if not row_id or decision not in CLAUDE_ALLOWED_DECISIONS:
            continue
        result[row_id] = {
            "decision": decision,
            "notes": str(item.get("notes", "")).strip(),
        }
    return result


def build_review_payload(
    rows: Sequence[WorkingRow],
    candidates: Sequence[int],
    ref_context: Dict[str, dict[str, str]],
) -> str:
    payload_rows = []
    for index in candidates:
        row = rows[index]
        neighbors = []
        for neighbor_index in range(max(0, index - 2), min(len(rows), index + 3)):
            if neighbor_index == index:
                continue
            neighbor = rows[neighbor_index]
            if not neighbor.is_speech():
                continue
            neighbors.append(
                {
                    "row_id": neighbor.row_id,
                    "text": neighbor.text,
                    "status": neighbor.status,
                    "reference": neighbor.reference_segment,
                }
            )
        payload_rows.append(
            {
                "row_id": row.row_id,
                "text": row.text,
                "status": row.status,
                "neighbors": neighbors,
                "left_reference": ref_context.get(row.row_id, {}).get("left_reference", ""),
                "right_reference": ref_context.get(row.row_id, {}).get("right_reference", ""),
            }
        )
    return json.dumps(payload_rows, ensure_ascii=False, indent=2)


def review_unmatched_rows(
    rows: Sequence[WorkingRow],
    candidate_indices: Sequence[int],
    ref_context: Dict[str, dict[str, str]],
    model: str = DEFAULT_CLAUDE_MODEL,
    max_tokens: int = 1200,
    api_key: str | None = None,
) -> Dict[str, dict[str, str]]:
    if not candidate_indices:
        return {}
    if anthropic is None:  # pragma: no cover
        print(
            "WARNING: `anthropic` package unavailable; defaulting unmatched rows to KEEP_UNMATCHED.",
            file=sys.stderr,
        )
        return {}
    try:
        client = anthropic.Anthropic(api_key=resolve_api_key(api_key))
    except Exception as exc:
        print(
            f"WARNING: unable to initialize Anthropic client ({exc}); defaulting unmatched rows to KEEP_UNMATCHED.",
            file=sys.stderr,
        )
        return {}
    payload = build_review_payload(rows, candidate_indices, ref_context)
    prompt = PROMPT_TEMPLATE.format(
        allowed=", ".join(CLAUDE_ALLOWED_DECISIONS),
        payload=payload,
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        print(
            f"WARNING: Anthropic stage-06 review failed ({exc}); defaulting unmatched rows to KEEP_UNMATCHED.",
            file=sys.stderr,
        )
        return {}
    return parse_review_response(extract_text(response))
