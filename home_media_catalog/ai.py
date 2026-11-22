"""Opt-in provider-independent prompt-to-filters search. Secrets stay server-side."""

import json
import os
import re
import tempfile
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field

from .ai_providers import (
    LANGUAGE_DEFAULTS,
    PROVIDERS,
    generate_json,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": ["string", "null"], "enum": ["movie", "tvshow", "episode", None]},
        "watch_status": {
            "type": ["string", "null"],
            "enum": ["watched", "unwatched", "in_progress", "unknown", None],
        },
        "genre": {"type": ["string", "null"]},
        "format": {"type": ["string", "null"]},
        "year_min": {"type": ["integer", "null"]},
        "year_max": {"type": ["integer", "null"]},
        "rating_min": {"type": ["number", "null"]},
        "user_rating_min": {"type": ["number", "null"]},
        "person_name": {"type": ["string", "null"]},
        "person_role": {"type": ["string", "null"]},
        "keywords": {"type": "string"},
        "semantic_query": {"type": "string"},
        "retrieval_terms": {"type": "array", "items": {"type": "string"}},
        "assistant_message": {"type": "string"},
    },
    "required": [
        "kind",
        "watch_status",
        "genre",
        "format",
        "year_min",
        "year_max",
        "rating_min",
        "user_rating_min",
        "person_name",
        "person_role",
        "keywords",
        "semantic_query",
        "retrieval_terms",
        "assistant_message",
    ],
    "additionalProperties": False,
}


class AISettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = "openai"
    model: str = Field(default="gpt-4o-mini", min_length=1, max_length=100)
    api_key: str | None = Field(default=None, max_length=300)
    clear_api_key: bool = False
    monthly_budget_usd: float | None = Field(default=None, ge=0, le=10000)
    # Accept old clients without using or provisioning an embedding model.
    embedding_provider: str | None = None
    embedding_model: str | None = Field(default=None, min_length=1, max_length=100)
    embedding_api_key: str | None = None
    clear_embedding_api_key: bool = False


class AISearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=3, max_length=500)
    previous_plan: "AISearchPlan | None" = None
    previous_ids: list[str] = Field(default_factory=list, max_length=60)


class AISearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str | None = None
    watch_status: str | None = None
    genre: str | None = None
    format: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    rating_min: float | None = None
    user_rating_min: float | None = None
    person_name: str | None = None
    person_role: str | None = None
    keywords: str = ""
    semantic_query: str = ""
    retrieval_terms: list[str] = Field(default_factory=list, max_length=16)
    assistant_message: str = ""


class AIConfiguration:
    """Dedicated 0600 JSON config in /data, outside catalogue backups and HTTP."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "ai-settings.json"
        self.lock = RLock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (FileNotFoundError, ValueError):
            pass
        return {}

    def _keys(self, value: dict) -> dict:
        # Upgrade pre-provider OpenAI settings without invalidating existing secrets.
        return {
            **({"openai": value["api_key"]} if value.get("api_key") else {}),
            **value.get("api_keys", {}),
        }

    def status(self) -> dict:
        with self.lock:
            value = self._read()
            provider = value.get("provider", "openai")
            if provider not in PROVIDERS:
                provider = "openai"
            keys = self._keys(value)
            return {
                "provider": provider,
                "model": value.get("model") or LANGUAGE_DEFAULTS[provider],
                "configured": bool(keys.get(provider)),
                "has_api_key": bool(keys.get(provider)),
                "configured_providers": sorted(name for name in PROVIDERS if keys.get(name)),
                "search_mode": "hybrid_vector" if provider == "openai" else "local_ai",
                "embedding_model": value.get("embedding_model") or "text-embedding-3-small",
                "monthly_budget_usd": value.get("monthly_budget_usd", 5.0),
                "revision": int(value.get("revision") or 0),
            }

    def credentials(self) -> tuple[str, str] | None:
        with self.lock:
            value = self._read()
            provider = value.get("provider", "openai")
            if provider not in PROVIDERS:
                return None
            key = self._keys(value).get(provider, "")
            return (value.get("model") or LANGUAGE_DEFAULTS[provider], key) if key else None

    def device_credential(self) -> dict:
        """Administrator-only secret export through the separate HTTPS endpoint."""
        with self.lock:
            data = self._read()
            provider = data.get("provider", "openai")
            if provider not in PROVIDERS:
                provider = "openai"
            return {
                "provider": provider,
                "model": data.get("model") or LANGUAGE_DEFAULTS[provider],
                "api_key": self._keys(data).get(provider) or None,
                "revision": int(data.get("revision") or 0),
            }

    def provision_device_credential(self, provider: str, model: str, key: str) -> bool:
        """Fill only an absent provider key; never overwrite server credentials."""
        if provider not in PROVIDERS or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", model):
            raise ValueError("Invalid AI provider or model.")
        if not 8 <= len(key) <= 300 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
            raise ValueError("Invalid AI key.")
        with self.lock:
            data = self._read()
            keys = self._keys(data)
            if keys.get(provider):
                return False
            keys[provider] = key
            previous = data.get("provider", "openai")
            if not any(value for name, value in keys.items() if name != provider):
                data["provider"] = provider
                data["model"] = model
            elif previous not in PROVIDERS:
                data["provider"] = provider
            data.pop("api_key", None)
            data["api_keys"] = keys
            data["revision"] = int(data.get("revision") or 0) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".ai-config-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(name, 0o600)
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return True

    def replace_device_credential(
        self, provider: str, model: str, key: str, expected_revision: int,
    ) -> dict:
        """Atomic, optimistic-concurrency update from an authenticated iPhone.

        This updates the SAME active provider/model/key used by server-side AI
        search. A stale iPhone never silently overwrites a newer web edit.
        """
        if provider not in PROVIDERS or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", model):
            raise ValueError("Invalid provider or model.")
        if key and (not 8 <= len(key) <= 4096 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key)):
            raise ValueError("Invalid AI key.")
        with self.lock:
            data = self._read()
            current_revision = int(data.get("revision") or 0)
            if expected_revision != current_revision:
                raise RuntimeError("AI configuration was modified on the server.")
            keys = self._keys(data)
            if key:
                keys[provider] = key
            else:
                keys.pop(provider, None)
            data.update({
                "provider": provider, "model": model,
                "api_keys": keys, "revision": current_revision + 1,
            })
            data.pop("api_key", None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".ai-config-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(name, 0o600)
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return {
                "provider": provider, "model": model,
                "api_key": key or None, "revision": current_revision + 1,
            }

    def update(self, request: AISettingsInput) -> dict:
        if request.provider not in PROVIDERS:
            raise ValueError("Unsupported AI provider.")
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", request.model) or (
            request.embedding_model is not None
            and not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", request.embedding_model)
        ):
            raise ValueError("Invalid model identifier.")
        if request.api_key is not None and request.api_key and not request.api_key.strip():
            raise ValueError("The access key must not be whitespace.")
        with self.lock:
            value = self._read()
            keys = self._keys(value)
            if request.clear_api_key:
                keys.pop(request.provider, None)
            if request.api_key:
                keys[request.provider] = request.api_key.strip()
            previous_provider = value.get("provider", "openai")
            model = (
                LANGUAGE_DEFAULTS[request.provider]
                if request.provider != previous_provider
                and request.model == LANGUAGE_DEFAULTS["openai"]
                else request.model
            )
            contents = json.dumps(
                {
                    "provider": request.provider,
                    "model": model,
                    "api_keys": keys,
                    "revision": int(value.get("revision") or 0) + 1,
                    # Keep previously built OpenAI vectors reusable after provider switches.
                    "embedding_model": request.embedding_model or value.get("embedding_model") or "text-embedding-3-small",
                    "monthly_budget_usd": (
                        request.monthly_budget_usd
                        if request.monthly_budget_usd is not None
                        else value.get("monthly_budget_usd", 5.0)
                    ),
                }
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".ai-config-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(contents)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(name, 0o600)
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return self.status()


def normalize_plan(plan: AISearchPlan, facets: dict) -> AISearchPlan:
    """Drop invented facets; all predicates are applied locally to SQLite."""
    values = plan.model_dump()
    for field, available in (
        ("genre", facets.get("genres", [])),
        ("format", facets.get("formats", [])),
    ):
        selected = values[field]
        values[field] = next(
            (v for v in available if selected and v.casefold() == selected.casefold()), None
        )
    if values["kind"] not in (None, "movie", "tvshow", "episode"):
        values["kind"] = None
    if values["watch_status"] not in (None, "watched", "unwatched", "in_progress", "unknown"):
        values["watch_status"] = None
    for field in ("year_min", "year_max"):
        if values[field] is not None and not 1800 <= values[field] <= 2200:
            values[field] = None
    for field in ("rating_min", "user_rating_min"):
        if values[field] is not None and not 0 <= values[field] <= 10:
            values[field] = None
    if values["person_role"] not in (None, "cast", "director", "composer", "writer"):
        values["person_role"] = None
    if values["person_name"] is not None:
        values["person_name"] = values["person_name"].strip()[:120] or None
    values["keywords"] = values["keywords"].strip()[:120]
    values["semantic_query"] = values["semantic_query"].strip()[:300]
    values["retrieval_terms"] = list(dict.fromkeys(
        str(term).strip()[:70] for term in values["retrieval_terms"]
        if str(term).strip()
    ))[:12]
    values["assistant_message"] = values["assistant_message"].strip()[:300]
    return AISearchPlan(**values)


async def interpret_prompt(
    prompt: str, model: str, key: str, facets: dict, previous: AISearchPlan | None = None,
    *, provider: str = "openai",
) -> AISearchPlan:
    instructions = (
        "You translate film library searches into strictly factual filters. "
        "Set a field to null when not explicitly requested. "
        "Use only available exact genre and format labels. You MAY infer a genre "
        "from a well-known cinematic style (e.g. Miyazaki implies animation/fantasy), "
        "but only when the catalogue offers a matching exact label. This is a hard filter "
        "to avoid false-positive stylistic matches; do not invent unavailable genres. "
        "An artistic/director/style reference is a strict relevance requirement, not "
        "just a loose theme. Prefer precision over recall and avoid loosely related movies. "
        "Do not invent a format such as HDR unless provided. "
        "Rating is on a 0-10 scale. Use rating_min for a minimum general "
        "rating and user_rating_min for a minimum personal rating. "
        "Extract a named actor, director, film composer or screenwriter into "
        "person_name and set person_role to cast, director, composer or writer. "
        "Use person_role=null when the credit is unspecified. "
        "For searches about a named person's credits, do not place their name "
        "in semantic_query or keywords unless an additional plot/theme is requested. "
        "Never confuse a film score composer with its cast. "
        "Set kind=episode for episode-specific requests; tvshow for series requests. "
        "Return semantic_query as a short searchable description of the plot, theme, "
        "atmosphere, or character requested, excluding constraints like year or format. "
        "For purely structured searches without a theme, semantic_query must be empty. "
        "Return retrieval_terms as 4 to 10 concise keyword phrases or synonyms useful "
        "to retrieve relevant film plots locally using full-text search. Include "
        "synonyms and related concrete concepts in the same language as the catalogue "
        "as well as English when useful. Do NOT add unrelated broad genre terms. "
        "For purely structured queries, retrieval_terms must be an empty list. "
        "Write assistant_message as one brief explanatory sentence in the user's language, "
        "without claiming that any particular matching film exists. "
        "Set keywords ONLY for an explicitly named title or unique quoted phrase "
        "that can be matched lexically in title/synopsis/cast; not for broad plots, moods or concepts. "
        "Never invent film titles. If previous filters are supplied, this is a conversational "
        "refinement: preserve previous fields and semantic query unless the new user "
        "request changes or removes them; output the complete merged plan. Respond in JSON per schema."
    )
    data = await generate_json(
        instructions,
        "Available genres: " + json.dumps(facets.get("genres", [])[:120], ensure_ascii=False)
        + "\\nAvailable formats: " + json.dumps(facets.get("formats", [])[:120], ensure_ascii=False)
        + "\\nPrevious search plan: "
        + (previous.model_dump_json(exclude={"assistant_message"}) if previous else "none")
        + "\\nUser search: " + prompt,
        SCHEMA, model, key, provider=provider,
    )
    return normalize_plan(AISearchPlan.model_validate(data), facets)



REFINEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {"type": "string", "enum": ["filter", "expand", "search"]},
        "keep_ids": {"type": "array", "items": {"type": "string"}},
        "assistant_message": {"type": "string"},
    },
    "required": ["operation", "keep_ids", "assistant_message"],
    "additionalProperties": False,
}


async def interpret_result_refinement(
    prompt: str, model: str, key: str, candidates: list[dict],
    *, provider: str = "openai",
) -> dict:
    """Decide whether the user is editing existing hits, without inventing catalogue IDs."""
    instructions = (
        "You manage an existing movie search result set. Choose operation=filter for "
        "instructions to remove, exclude, keep only, sort down to a number, or narrow "
        "the CURRENT results (e.g. 'retirer Valérian de la liste', 'only unwatched'). "
        "For filter, return keep_ids as the subset of provided IDs matching the "
        "instruction, preserving the supplied order unless explicitly asked to rank. "
        "Never add new IDs when filtering. A removal only removes its matching entries "
        "and preserves all other items. Choose operation=expand when asked to add "
        "more, find others, extend the current list, or show additional results "
        "(including 'ajoutes en d'autres'); use keep_ids=[]. Expand retains the "
        "existing search criteria and its current results. Choose operation=search "
        "for a genuinely new search or replacement of the query, or when current "
        "metadata is insufficient to filter; use keep_ids=[]. "
        "Do not filter by details that are not supplied in candidate data. "
        "Return a concise assistant_message in the user's language. "
        "Use only titles and metadata actually supplied."
    )
    value = await generate_json(
        instructions,
        "Current results: " + json.dumps(candidates, ensure_ascii=False)
        + "\\nUser instruction: " + prompt,
        REFINEMENT_SCHEMA, model, key, provider=provider, max_output_tokens=3400,
        schema_name="result_refinement",
    )
    if value.get("operation") not in ("filter", "expand", "search"):
        raise ValueError("Invalid refinement operation.")
    allowed = {item["id"] for item in candidates}
    selected = set(value.get("keep_ids", [])) & allowed
    value["keep_ids"] = [item["id"] for item in candidates if item["id"] in selected]
    return value
