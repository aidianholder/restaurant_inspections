"""Shorten an export's observations with OpenAI.

Called only from `views.output_summary`, which means the API token never leaves
the server: the browser posts the county and date range to us, we rebuild the
export from our own data and send *that*. A summarise button that called OpenAI
directly would have to ship the newsroom's token to every reader of the page.

The prompt is deliberately narrow. These are quotes from a public record, and a
model that helpfully explains what a violation *means* would be putting words in
an inspector's mouth, so it is told repeatedly to use nothing but the text it was
handed.

`SYSTEM_PROMPT` below is the wording that ships, seeded into `SummaryPrompt` by
migration so it can be tuned in the admin without a deploy. It stays here as the
fallback: an install that has never run the seed, or a table someone has emptied,
summarises with this rather than failing.
"""

import logging
import re

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class SummaryError(Exception):
    """Anything that stopped a summary coming back, worded for the screen."""


SYSTEM_PROMPT = """\
You are preparing a newspaper's health inspection roundup for publication. The \
user message is an HTML fragment: a heading, a few explanatory paragraphs, \
then every food service establishment inspected in that period in \
alphabetical order, with the violations each one was cited for grouped under \
"Priority", "Priority Foundation" and "Core" headings.

For each establishment, summarise each group of violations separately. Replace \
the list items under a group heading with a single list item containing one \
shortened summary of all the observations in that group.

Rules:

1. Use only the information in the text you were given. Do not infer, assume or \
add anything else you know about these violations, these establishments, food \
safety, or the health code. If a detail is not in the source text, it does not \
go in the summary.
2. Summarise each group on its own. A "Priority" summary covers only that \
establishment's priority observations, and likewise for "Priority Foundation" \
and "Core". Never merge observations across groups or across establishments.
3. Do not combine violations -- each violation should be considered separately. \
You can combine multiple violations into a single sentence, but it should always \
be clear that they are separate violations.
4. Do not skip violations - each violation should be included in the summary.
5. Keep everything else exactly as it appears: the heading at the top, the \
explanatory paragraphs below it, every establishment name, address and \
inspection type, and every group heading. Keep the establishments in the order \
they are given.
6. Return valid HTML using the same tags and structure as the input, changing \
only the contents of the lists.
7. Return the HTML and nothing else — no Markdown code fences, no preamble, no \
closing remarks.\
"""

# What the seeded row is called, and what the screen reports when no row exists.
SHIPPED_PROMPT_NAME = "Default"

# Models return fenced HTML often enough to be worth handling rather than
# pasting ```html into a story.
_FENCE = re.compile(r"\A\s*```[a-zA-Z]*\s*\n(.*?)\n?\s*```\s*\Z", re.S)


class Summary:
    """The shortened HTML, plus what the screen needs to say about it."""

    def __init__(self, html, model="", truncated=False, prompt=SHIPPED_PROMPT_NAME):
        self.html = html
        self.model = model
        self.truncated = truncated
        # Which wording produced this. Without it, comparing two summaries means
        # guessing which prompt made either.
        self.prompt = prompt

    def __str__(self):
        return self.html


def _unfence(text):
    match = _FENCE.match(text)
    return (match.group(1) if match else text).strip()


def _api_error(response):
    """OpenAI's own message where there is one, the status code otherwise."""
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = f"HTTP {response.status_code}"
    if response.status_code == 401:
        return f"OpenAI rejected the token (OPEN_AI_TOKEN): {message}"
    if response.status_code == 429:
        return f"OpenAI is rate-limiting or the account is out of quota: {message}"
    return f"OpenAI returned an error: {message}"


def summarize(html, token=None, model=None, timeout=None, session=None, prompt=None):
    """Send `html` to OpenAI and return the shortened version.

    Raises `SummaryError` for anything a person could act on — no token, a
    refused key, an unreachable API — so the view has one thing to catch.

    `prompt` is a `SummaryPrompt`; omitted, the active row is read at call time.
    Deliberately not cached: one query against a table of a few rows, next to a
    call that takes half a minute, buys nothing — and a stale prompt would mean
    "I changed it in the admin and nothing happened", which is the whole failure
    this is meant to prevent.
    """
    token = token if token is not None else settings.OPENAI_TOKEN
    if not token:
        raise SummaryError(
            "No OpenAI token is configured. Set OPEN_AI_TOKEN in the environment "
            "and restart the site."
        )
    if not html.strip():
        raise SummaryError("There is nothing to summarise.")

    if prompt is None:
        from .models import SummaryPrompt

        prompt = SummaryPrompt.active()

    system_message = prompt.system_prompt if prompt else SYSTEM_PROMPT
    user_message = prompt.render_user_message(html) if prompt else html
    prompt_name = prompt.name if prompt else SHIPPED_PROMPT_NAME
    if prompt is None:
        logger.info("No active summary prompt; using the wording shipped in code.")

    payload = {
        "model": model or (prompt.model if prompt else "") or settings.OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        # No temperature and no token cap on purpose: which of those two the API
        # accepts changes between model generations, and sending one the chosen
        # model rejects fails the whole call. The defaults suit this job.
    }

    try:
        response = (session or requests).post(
            settings.OPENAI_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout or settings.OPENAI_TIMEOUT,
        )
    except requests.Timeout as exc:
        logger.warning("OpenAI timed out: %s", exc)
        raise SummaryError(
            "OpenAI took too long to answer. Try a narrower date range."
        ) from exc
    except requests.RequestException as exc:
        # urllib3's own message is a wall of retry internals — useful in the log,
        # useless on screen.
        logger.warning("OpenAI request failed: %s", exc)
        raise SummaryError(
            "Couldn't reach OpenAI. Check the server's network connection."
        ) from exc

    if response.status_code != 200:
        message = _api_error(response)
        logger.warning("OpenAI %s: %s", response.status_code, message)
        raise SummaryError(message)

    try:
        choice = response.json()["choices"][0]
        text = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise SummaryError("OpenAI's response wasn't in the expected shape.") from exc

    if not (text or "").strip():
        raise SummaryError("OpenAI returned an empty summary.")

    return Summary(
        _unfence(text),
        model=response.json().get("model", payload["model"]),
        # The roundup runs long, and a summary that stops mid-establishment
        # looks finished unless we say otherwise.
        truncated=choice.get("finish_reason") == "length",
        prompt=prompt_name,
    )
