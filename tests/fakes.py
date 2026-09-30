"""A scripted stand-in for a model backend.

It is a ``Backend`` like the real ones in ``app/llm/backends``, so everything
above the transport layer runs for real. Each
response schema has a default answer derived from the prompt it was sent; a
test swaps one in with ``respond()``. Every call is recorded, so a test can
assert on exactly what was sent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from resumap.llm.backends import Backend, Completion, Usage

Handler = Callable[[str, str], "dict[str, Any] | None"]

# Terms the default JD tagger reports when a point contains them, plus one it
# always invents so the substring filter in claude._apply_tags has work to do.
TECH_TERMS = ("Kafka", "Python", "Spark", "PostgreSQL", "Airflow", "React")
INVENTED_TERM = "Kubernetes"

REPLACED_TEXT = "Delivered React dashboards for analysts, backed by FastAPI and PostgreSQL."
EXPERIENCE_TEXT = "Built an open-source CLI in Go that validates Airflow DAGs before deploy."


@dataclass
class Call:
    schema: str
    system: str
    user: str
    model: str


def between(text: str, tag: str) -> str:
    """The content of the first <tag>...</tag> in a prompt."""
    m = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", text, re.S)
    return m.group(1).strip() if m else ""


def tailored(original: str) -> str:
    """The default rewrite: the original with its first word upper-cased.

    Same length, so the layout does not move, and still easy to spot in a PDF.
    """
    first, _, rest = original.partition(" ")
    return f"{first.upper()} {rest}"


def _ping(system: str, user: str) -> dict:
    return {"ok": True, "model": "fake-model"}


def _tags(system: str, user: str) -> dict:
    points = re.findall(r"^(jd\d+): (.*)$", between(user, "points"), re.M)
    return {
        "company": "Contoso",
        "role": "Data Engineer",
        "tags": [
            {
                "id": pid,
                "category": "requirement",
                "keywords": [t for t in TECH_TERMS if t in text] + [INVENTED_TERM],
            }
            for pid, text in points
        ],
    }


def _rewrite(system: str, user: str) -> dict:
    original = between(user, "original_bullet")
    if "<other_bullets>" in user:  # claude.replace_request
        text = REPLACED_TEXT
    else:
        text = tailored(original)
    return {
        "rewritten": text,
        "keywords_used": ["Kafka"],
        "substitutions": [
            {
                "original": "",
                "replacement": "Kafka",
                "grounded": False,
                "note": "named in the JD point",
            }
        ],
        "defensible": True,
    }


def _new_bullet(system: str, user: str) -> dict:
    return {
        "bullet": EXPERIENCE_TEXT,
        "keywords_used": ["Airflow"],
        "substitutions": [],
        "defensible": True,
        "placement": "new",
        "new_section": "Open Source",
    }


def _outreach(system: str, user: str) -> dict:
    return {
        "linkedin_subject": "Data Engineer role at Contoso",
        "linkedin": "Hi Sam, I build streaming pipelines in Kafka and Python and would "
        "like to talk about the Data Engineer role at Contoso.",
        "email_subject": "Following up on the Data Engineer application",
        "email_body": "Hi Sam,\n\nI applied for the Data Engineer role this week.\n\nJane",
        "highlights_used": ["streaming ingestion pipeline"],
    }


def _revision(system: str, user: str) -> dict:
    return {"reply": "Nothing to change.", "edits": [], "layout": [], "justify": "unchanged"}


DEFAULTS: dict[str, Handler] = {
    "Ping": _ping,
    "JDTags": _tags,
    "Rewrite": _rewrite,
    "NewBullet": _new_bullet,
    "Outreach": _outreach,
    "PdfRevision": _revision,
}


class FakeBackend(Backend):
    name = "fake"
    label = "Fake"
    concurrency = 4

    def __init__(self) -> None:
        super().__init__(default_model="fake-model")
        self.calls: list[Call] = []
        self.handlers: dict[str, Handler] = dict(DEFAULTS)
        # A reason string makes the backend report itself unavailable.
        self.down: str | None = None

    def respond(self, schema: str, handler: Handler | dict | None) -> None:
        """Answer `schema` with `handler(system, user)`, a fixed dict, or None
        (a reply that does not validate)."""
        if handler is None or isinstance(handler, dict):
            value = handler
            self.handlers[schema] = lambda system, user: value
        else:
            self.handlers[schema] = handler

    def calls_for(self, schema: str) -> list[Call]:
        return [c for c in self.calls if c.schema == schema]

    def unavailable(self) -> str | None:
        return self.down

    async def complete(
        self, *, model: str, system: str, user: str, schema: type, max_tokens: int, effort: str
    ) -> Completion:
        self.calls.append(Call(schema.__name__, system, user, model))
        value = self.handlers[schema.__name__](system, user)
        return Completion(
            parsed=None if value is None else schema.model_validate(value),
            text=json.dumps(value),
            usage=Usage(input_tokens=100, output_tokens=20),
            request_id=f"fake-{len(self.calls)}",
        )
