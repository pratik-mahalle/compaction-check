"""Small native TypeSafe HTTP client; no chat model or generated explanations."""

import json
import math
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path

from .inputs import InputError, strict_json

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.13.0"


class ProviderError(RuntimeError):
    """A sanitized error safe to show in CI, without response bodies or credentials."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _ssl_context():
    try:
        context = ssl.create_default_context()
        # Some Homebrew Python installations have no OpenSSL CA bundle configured.
        # Use the system trust bundle if the default store is empty; never disable TLS verification.
        system_bundle = Path("/etc/ssl/cert.pem")
        if not context.get_ca_certs() and system_bundle.is_file():
            context.load_verify_locations(cafile=str(system_bundle))
        return context
    except (OSError, ValueError):
        raise ProviderError("Could not load a trusted TLS certificate bundle.") from None


@dataclass(frozen=True)
class Choice:
    choice: str
    confidence: float
    probabilities: dict[str, float]


@dataclass(frozen=True)
class Decision:
    answers: dict[str, Choice]
    model: str
    usage: dict


def unit_number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def parse_decision(data, questions) -> Decision:
    if not isinstance(data, dict) or not isinstance(data.get("model"), str) or not data["model"]:
        raise ProviderError("Jev returned an invalid response model.")
    answers = data.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ProviderError("Jev did not answer exactly the requested questions.")
    parsed = {}
    for name, question in questions.items():
        answer = answers[name]
        options = question["criteria"]
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            raise ProviderError("Jev returned an invalid choice answer.")
        choice = answer.get("choice")
        probs = answer.get("probabilities")
        confidence = answer.get("confidence")
        if not isinstance(choice, str) or choice not in options or not unit_number(confidence):
            raise ProviderError("Jev returned an unknown choice or invalid confidence.")
        if (
            not isinstance(probs, dict)
            or set(probs) != set(options)
            or not all(unit_number(v) for v in probs.values())
        ):
            raise ProviderError("Jev returned an invalid probability distribution.")
        # Jev can round to two decimal places. Tolerance covers accumulated rounding.
        if abs(sum(probs.values()) - 1) > max(0.02, 0.005 * len(probs)) + 1e-9:
            raise ProviderError("Jev probabilities do not sum to one within rounding tolerance.")
        if probs[choice] + 0.011 < max(probs.values()):
            raise ProviderError("Jev's selected choice disagrees with its probabilities.")
        parsed[name] = Choice(choice, float(confidence), probs)
    usage = data.get("usage", {})
    if not isinstance(usage, dict):
        raise ProviderError("Jev returned invalid usage metadata.")
    clean_usage = {}
    for name in ("input_tokens", "output_tokens"):
        value = usage.get(name)
        if value is not None:
            if type(value) is not int or value < 0:
                raise ProviderError("Jev returned invalid token counts.")
            clean_usage[name] = value
    return Decision(parsed, data["model"], clean_usage)


def _retry_delay(header, attempt):
    delay = min(2**attempt, 8)
    if header:
        try:
            candidate = float(header)
        except ValueError:
            try:
                candidate = (
                    parsedate_to_datetime(header) - datetime.now(timezone.utc)
                ).total_seconds()
            except (TypeError, ValueError, OverflowError):
                candidate = delay
        if math.isfinite(candidate):
            delay = max(delay, candidate)
    if delay > 30:
        raise ProviderError("Jev requested a retry delay over 30 seconds; try again later.")
    return max(0, delay)


@dataclass
class JevClient:
    api_key: str = field(default_factory=lambda: os.environ.get("TYPESAFE_API_KEY", ""), repr=False)
    model: str = DEFAULT_MODEL
    timeout: float = 30
    retries: int = 2

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise InputError("Set TYPESAFE_API_KEY in your environment to run a live check.")
        if not self.api_key.isascii() or any(not 33 <= ord(c) <= 126 for c in self.api_key):
            raise InputError(
                "TYPESAFE_API_KEY must contain only printable ASCII without whitespace."
            )
        if (
            not isinstance(self.model, str)
            or not self.model.strip()
            or len(self.model) > 128
            or any(ord(c) < 32 for c in self.model)
        ):
            raise InputError("Model must be a nonblank identifier of at most 128 characters.")
        if (
            type(self.timeout) not in (int, float)
            or not math.isfinite(self.timeout)
            or not 0 < self.timeout <= 60
        ):
            raise InputError("Timeout must be between 0 and 60 seconds.")
        if type(self.retries) is not int or not 0 <= self.retries <= 3:
            raise InputError("Retries must be between 0 and 3.")

    def evaluate(self, state, questions) -> Decision:
        payload = json.dumps(
            {"model": self.model, "state": state, "questions": questions},
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        opener = urllib.request.build_opener(
            _NoRedirect(), urllib.request.HTTPSHandler(context=_ssl_context())
        )
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(
                ENDPOINT,
                data=payload,
                method="POST",
                headers={
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(1_000_001)
                    if len(raw) > 1_000_000:
                        raise ProviderError("Jev response exceeded the 1 MB application limit.")
                try:
                    data = strict_json(raw.decode("utf-8"))
                except (InputError, UnicodeError):
                    raise ProviderError("Jev returned invalid JSON.") from None
                return parse_decision(data, questions)
            except urllib.error.HTTPError as exc:
                status, retry_after = exc.code, exc.headers.get("Retry-After")
                exc.close()
                if status in {429, 529} and attempt < self.retries:
                    time.sleep(_retry_delay(retry_after, attempt))
                    continue
                descriptions = {
                    401: "Authentication failed; check TYPESAFE_API_KEY.",
                    403: "The API key cannot access this resource.",
                    422: "Jev rejected the model or request shape.",
                    429: "Jev rate limit exceeded.",
                    529: "Jev is overloaded.",
                }
                raise ProviderError(
                    f"HTTP {status}: " + descriptions.get(status, "Jev request failed.")
                ) from None
            except (urllib.error.URLError, TimeoutError, OSError, HTTPException):
                # Do not automatically replay ambiguous network failures (possible extra billing).
                raise ProviderError(
                    "Could not reach Jev or the request timed out; no automatic replay."
                ) from None
        raise ProviderError("Jev retry budget exhausted.")
