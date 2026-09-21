"""Step extraction backends: Anthropic (Claude), Ollama (local), and the prompt they share."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from .util import die, log, warn
from .work_progress import report_work

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_CLAUDE_CLI_MODEL = "opus"
DEFAULT_OLLAMA_MODEL = "qwen2.5:14b"
DEFAULT_OLLAMA_HOST = "http://localhost:11434"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """The backend refused, failed, or returned something unusable."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class ProviderBlocked(LLMError):
    """A funding/spend limit needs intervention, rather than transient retries."""

    def __init__(self, message, *, reason="credits"):
        super().__init__(message)
        self.reason = reason


def funding_error(message: str, *, status=None, body=None, cli=False):
    """Recognize explicit billing errors; an ordinary 429 is still retryable.

    Structured Anthropic errors: https://platform.claude.com/docs/en/api/errors
    Spend limits: https://platform.claude.com/docs/en/api/rate-limits
    Text fallbacks cover older API responses and CLI error envelopes only.
    """
    error = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    details = error.get("details") or {}
    code = details.get("error_code") if isinstance(details, dict) else None
    text = str(error.get("message") or message).lower()
    reason = None
    if isinstance(code, str) and code in {"enforced_spend_limit_reached", "spend_limit_exceeded", "insufficient_quota"}:
        reason = "spend_limit"
    elif re.search(r"credit balance (?:is )?(?:too )?low|insufficient[_ ]credits?|out of credits|credits? (?:exhausted|depleted)", text):
        reason = "credits"
    elif re.search(r"(?:spend(?:ing)?|monthly cost) (?:limit|cap).{0,60}(?:reached|exceeded)|(?:reached|exceeded).{0,60}(?:spend(?:ing)?|monthly cost) (?:limit|cap)|insufficient_quota|reached your specified (?:workspace )?api usage limits", text):
        reason = "spend_limit"
    elif cli and re.search(r"usage limit (?:reached|exceeded)|(?:you've|you’ve|you have) hit your (?:usage )?limit", text):
        reason = "spend_limit"
    elif status == 402 or error.get("type") == "billing_error":
        reason = "billing"
    if reason:
        messages = {"credits": "The provider reports insufficient API credits. Add credits, then resume requests.",
                    "spend_limit": "The provider's spending or usage limit was reached. Restore access, then resume requests.",
                    "billing": "The provider reports a billing problem. Resolve it, then resume requests."}
        return ProviderBlocked(messages[reason], reason=reason)
    return None


SYSTEM_PROMPT = """\
You convert transcripts of screen-recorded software tutorials into a condensed, \
skimmable, step-by-step written guide. The reader wants to reproduce what the \
instructor did, at their own pace, without watching the video.

Return a single JSON object and nothing else:

{
  "title": "string",
  "summary": "string",
  "prerequisites": ["string"],
  "skip": [{"start": 0.0, "end": 0.0, "reason": "string"}],
  "steps": [
    {
      "title": "string",
      "start": 0.0,
      "end": 0.0,
      "actions": ["string"],
      "shortcuts": ["string"],
      "note": "string",
      "motion": true
    }
  ]
}

Field rules:
- title: what this lesson produces or teaches. Under 70 characters.
- summary: 1-3 sentences naming what the reader will have built or be able to do by
  the end. Name the thing itself, not the topic: "you block out the base shape of a
  low-poly pickup", not "this lesson covers modelling".
- prerequisites: everything that must already be true before step 1 works. Add-ons that
  must be enabled, files or textures to download, preferences or overlay settings to
  change, and results carried over from an earlier lesson. Include these even when the
  instructor mentions them in passing partway through rather than at the start, and say
  which they are specifically ("the Loop Tools add-on enabled", not "some add-ons").
  [] only when the lesson genuinely needs nothing.
- skip: time ranges that teach nothing and must not become steps. Promotions for the
  instructor's own courses, sponsor reads, "link in the description", Patreon and
  subscribe appeals, channel intros, and sign-offs all belong here. Give each a short
  reason. [] when the lesson has none.
- steps[].title: an imperative phrase naming the outcome, under 70 characters.
  "Add a Subdivision Surface modifier", not "Modifiers".
- steps[].start and steps[].end: seconds, taken from the transcript timestamps.
  Ascending, non-overlapping, covering the lesson in order.
- steps[].actions: 1-6 short imperative instructions, each one thing the reader does.
- steps[].shortcuts: keyboard shortcuts and menu paths named in this step, verbatim
  ("Ctrl+1", "Add > Mesh > Cylinder"). [] when none.
- steps[].note: one gotcha, default value, or piece of context worth keeping. "" when none.
- steps[].motion: true when the step is a continuous manipulation whose movement matters
  and a still frame would not convey it: dragging, sliding a loop cut, extruding,
  scaling, rotating, sculpting, orbiting to inspect. false for a click, a menu choice,
  a typed value, or a toggle. Most steps are false.

Editing rules:
- Cover the whole transcript except the ranges you put in skip. Do not stop early.
- Prefer many small steps over a few large ones. Every distinct action gets its own
  step: a step that bundles unrelated actions is too big. A 20 minute lesson usually
  yields 25-40 steps, and a step rarely spans more than about 60 seconds.
- Never write a step whose time range sits inside a skip range.
- Cut greetings, sign-offs, sponsor reads, asides, repetition, and thinking out loud.
  Keep everything needed to reproduce the result.
- Keep exact values verbatim: numbers, units, axis names, object and file names, menu
  paths, shortcuts, property names. Never round or paraphrase a number.
- Write instructions to the reader ("Press Tab to enter Edit Mode"), never narration
  ("he presses Tab").
- Prefer the instructor's corrections over their first attempt. If they undo something,
  describe what they settled on.
- Transcripts contain speech-to-text errors. Silently correct mangled tool names and
  shortcuts when context makes the intent clear.

Output JSON only. No markdown fences, no commentary before or after.\
"""


def build_user_prompt(
    *,
    title: str,
    duration: float,
    transcript_lines: list[str],
    scene_times: list[float],
    window: tuple[float, float] | None = None,
    promo_hints: list[tuple[float, float]] | None = None,
) -> str:
    parts = [f"Lesson file title: {title}", f"Full video duration: {duration:.0f} seconds"]
    if window:
        start, end = window
        parts.append(
            f"You are given seconds {start:.0f}-{end:.0f} of a longer lesson. "
            f"Write steps for this portion only, using absolute timestamps. "
            f"Do not write an introduction or a conclusion for the whole lesson."
        )
    if scene_times:
        sampled = scene_times if len(scene_times) <= 400 else scene_times[:: len(scene_times) // 400 + 1]
        joined = ", ".join(f"{t:.1f}" for t in sampled)
        parts.append(
            "Timestamps where the screen changed noticeably (useful for step boundaries): "
            + joined
        )
    if promo_hints:
        ranges = ", ".join(f"{start:.0f}-{end:.0f}" for start, end in promo_hints)
        parts.append(
            "These ranges look promotional to a keyword scan, which is often wrong. "
            f"Judge them yourself and put the genuine ones in skip: {ranges}"
        )
    parts.append(
        "Transcript, one line per segment, prefixed with its start time in seconds:\n\n"
        + "\n".join(transcript_lines)
    )
    return "\n\n".join(parts)


def extract_json(text: str) -> dict:
    """Parse a JSON object out of model output that may carry fences or stray prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    cleaned = cleaned.strip().strip("`").strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    if start == -1:
        raise LLMError("model returned no JSON object")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start : index + 1])
                except json.JSONDecodeError as exc:
                    raise LLMError(f"model returned malformed JSON: {exc}") from exc
    raise LLMError("model returned a truncated JSON object")


class AnthropicBackend:
    """Claude via the official SDK. Imported lazily so the rest of the tool needs no deps."""

    name = "anthropic"

    def __init__(self, model: str | None = None, *, max_tokens: int = 32000, fallbacks: bool = True):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            die(
                "the 'anthropic' package is not installed.\n"
                "  Use `nix develop` / `nix run` from this repo, or `pip install anthropic`,\n"
                "  or pick another backend: --llm ollama / --llm heuristic"
            )
        import anthropic

        self._anthropic = anthropic
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        self.max_tokens = max_tokens
        self.fallbacks = fallbacks
        self._fallbacks_unavailable = False
        try:
            self._client = anthropic.Anthropic()
        except Exception as exc:  # missing credentials surface here
            die(
                f"could not create the Anthropic client: {exc}\n"
                "  Set ANTHROPIC_API_KEY, or run `ant auth login`."
            )

    def _final_message(self, system: str, user: str):
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self.fallbacks and not self._fallbacks_unavailable:
            try:
                with self._client.beta.messages.stream(
                    betas=[FALLBACK_BETA],
                    extra_body={"fallbacks": "default"},
                    **kwargs,
                ) as stream:
                    return self._collect_stream(stream)
            except Exception as exc:
                if not self._is_unsupported(exc):
                    raise
                warn(
                    "server-side refusal fallbacks are unavailable with this SDK/API "
                    "version; continuing without them"
                )
                self._fallbacks_unavailable = True
        with self._client.messages.stream(**kwargs) as stream:
            return self._collect_stream(stream)

    @staticmethod
    def _collect_stream(stream):
        characters = 0
        for chunk in stream.text_stream:
            characters += len(chunk)
            report_work("model", "Receiving instructions", detail=f"{characters:,} characters received")
        return stream.get_final_message()

    @staticmethod
    def _is_unsupported(exc: Exception) -> bool:
        if isinstance(exc, TypeError):
            return True
        text = str(exc).lower()
        return "beta" in text or "fallback" in text or "unexpected keyword" in text

    def complete(self, system: str, user: str) -> str:
        try:
            message = self._final_message(system, user)
        except self._anthropic.APIStatusError as exc:
            blocked = funding_error(str(exc), status=exc.status_code, body=getattr(exc, "body", None))
            if blocked:
                raise blocked from exc
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc}",
                           retryable=exc.status_code == 429 or exc.status_code >= 500) from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach the Anthropic API: {exc}", retryable=True) from exc
        except self._anthropic.APIError as exc:
            # Streaming error events may arrive after an HTTP 200 response.
            blocked = funding_error(str(exc), body=getattr(exc, "body", None))
            if blocked:
                raise blocked from exc
            raise LLMError(f"Anthropic streaming error: {exc}") from exc

        stop_reason = getattr(message, "stop_reason", None)
        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMError(f"the model declined this transcript (stop_details={details})")
        if stop_reason == "max_tokens":
            raise LLMError(
                "response hit max_tokens before finishing. Open Processing settings in Task queue "
                "and lower the section length, then retry (CLI: --chunk-minutes)."
            )

        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        if not text.strip():
            raise LLMError("the model returned an empty response")
        return text


class OllamaBackend:
    """A local model served by Ollama. No API key, no network egress."""

    name = "ollama"

    def __init__(self, model: str | None = None, *, host: str | None = None, timeout: float = 900.0):
        self.model = model or os.environ.get("V2W_OLLAMA_MODEL") or DEFAULT_OLLAMA_MODEL
        self.host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")
        if not self.host.startswith("http"):
            self.host = f"http://{self.host}"
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "options": {"temperature": 0.2, "num_ctx": 32768},
            }
        ).encode()
        request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", errors="replace")
            blocked = funding_error(detail, status=exc.code)
            if blocked:
                raise blocked from exc
            raise LLMError(f"Ollama returned HTTP {exc.code}: {detail}",
                           retryable=exc.code == 429 or exc.code >= 500) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LLMError(f"could not reach Ollama at {self.host}: {exc}", retryable=True) from exc

        text = (body.get("message") or {}).get("content") or ""
        if not text.strip():
            raise LLMError("Ollama returned an empty response")
        return text


class ClaudeCliBackend:
    """Claude through the `claude` CLI, which uses a Claude subscription.

    This is the backend to use when you have no API key. Note that --bare is
    deliberately not passed: it skips keychain reads, which is where a
    subscription login lives.
    """

    name = "claude-cli"

    def __init__(
        self,
        model: str | None = None,
        *,
        binary: str | None = None,
        timeout: float = 1800.0,
    ):
        self.binary = binary or os.environ.get("V2W_CLAUDE_BIN") or "claude"
        if not shutil.which(self.binary):
            die(
                f"'{self.binary}' is not on PATH.\n"
                "  Install Claude Code, or choose another backend "
                "(--llm anthropic / ollama / heuristic)."
            )
        self.model = model or os.environ.get("V2W_CLAUDE_MODEL") or DEFAULT_CLAUDE_CLI_MODEL
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        command = [
            self.binary,
            "--print",
            "--restricted",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--allowed-tools",
            "",
            "--system-prompt",
            system,
        ]
        try:
            proc = subprocess.run(
                command,
                input=user,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"the claude CLI did not finish within {self.timeout:.0f}s") from exc
        except OSError as exc:
            raise LLMError(f"could not run '{self.binary}': {exc}") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-500:]
            if blocked := funding_error(detail, cli=True):
                raise blocked
            raise LLMError(f"the claude CLI exited {proc.returncode}: {detail}")

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"unreadable claude CLI output: {proc.stdout[:300]!r}") from exc

        result = payload.get("result") or ""
        # subtype stays "success" even for auth failures, so is_error is the real signal.
        if payload.get("is_error"):
            if blocked := funding_error(result, body=payload, cli=True):
                raise blocked
            raise LLMError(f"the claude CLI reported an error: {result[:300]}")
        if not result.strip():
            raise LLMError("the claude CLI returned an empty result")
        return result


class CodexCliBackend:
    """OpenAI Codex CLI, for a Codex subscription. Untested: no credits available here."""

    name = "codex-cli"

    def __init__(self, model: str | None = None, *, binary: str | None = None, timeout: float = 1800.0):
        self.binary = binary or os.environ.get("V2W_CODEX_BIN") or "codex"
        if not shutil.which(self.binary):
            die(f"'{self.binary}' is not on PATH. Install the Codex CLI or pick another backend.")
        self.model = model or os.environ.get("V2W_CODEX_MODEL")
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        command = [self.binary, "exec", "--skip-git-repo-check"]
        if self.model:
            command += ["--model", self.model]
        command.append("-")
        try:
            proc = subprocess.run(
                command,
                input=f"{system}\n\n---\n\n{user}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"the codex CLI did not finish within {self.timeout:.0f}s") from exc
        except OSError as exc:
            raise LLMError(f"could not run '{self.binary}': {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-500:]
            if blocked := funding_error(detail, cli=True):
                raise blocked
            raise LLMError(f"the codex CLI exited {proc.returncode}: {detail}")
        if not proc.stdout.strip():
            raise LLMError("the codex CLI returned nothing")
        return proc.stdout


def _ollama_reachable(host: str = DEFAULT_OLLAMA_HOST) -> bool:
    try:
        with urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=1.5):
            return True
    except Exception:
        return False


def _anthropic_credentials_present() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return (config / "anthropic").is_dir()


def make_backend(kind: str, model: str | None = None, *, fallbacks: bool = True):
    """Build a backend. 'auto' picks Anthropic if credentials exist, else a running Ollama."""
    if kind == "auto":
        if _anthropic_credentials_present():
            kind = "anthropic"
        elif shutil.which(os.environ.get("V2W_CLAUDE_BIN") or "claude"):
            kind = "claude-cli"
        elif _ollama_reachable():
            kind = "ollama"
        else:
            die(
                "no step-extraction backend available.\n"
                "  Install Claude Code to use a Claude subscription (--llm claude-cli),\n"
                "  set ANTHROPIC_API_KEY for the API (--llm anthropic),\n"
                "  start Ollama for a fully local model (--llm ollama),\n"
                "  or pass --llm heuristic to segment without any model."
            )
        log(f"step extraction backend: {kind} (auto-detected)")

    if kind == "anthropic":
        return AnthropicBackend(model, fallbacks=fallbacks)
    if kind == "claude-cli":
        return ClaudeCliBackend(model)
    if kind == "codex-cli":
        return CodexCliBackend(model)
    if kind == "ollama":
        return OllamaBackend(model)
    raise ValueError(f"unknown backend: {kind}")
