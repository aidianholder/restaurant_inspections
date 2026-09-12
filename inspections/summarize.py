"""Shorten an export's observations with OpenAI.

Called only from `views.output_summary`, which means the API token never leaves
the server: the browser posts the county and date range to us, we rebuild the
export from our own data and send *that*. A summarise button that called OpenAI
directly would have to ship the newsroom's token to every reader of the page.

Three things keep the document intact across the round trip:

* **The preamble is never sent.** The heading and the four explanatory
  paragraphs are ours and fixed, so they are held back and re-attached
  afterwards. A model cannot reword what it was never shown.
* **Establishments go in batches.** We generated the document, so we know where
  every block begins and can cut on seams we made rather than asking a model to
  preserve them. A wide date range becomes several small calls instead of one
  that times out.
* **What comes back is reconciled against what went in.** Every establishment
  sent is expected back; the ones that are not are named on screen. Dropping an
  establishment is the failure this has actually hit, and a quiet short document
  is the worst way to find out.

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
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings

from .output import establishment_names

logger = logging.getLogger(__name__)


class SummaryError(Exception):
    """Anything that stopped a summary coming back, worded for the screen."""


SYSTEM_PROMPT = """\
You are preparing a newspaper's health inspection roundup for publication. The \
user message is Markdown: one or more food service establishments, each with its \
name as a heading, then its address, then the inspection type, then the \
violations it was cited for grouped under bold "Priority", "Priority Foundation" \
and "Core" labels.

For each establishment, summarise each group of violations separately. Replace \
the bullets under a group label with a single bullet containing one shortened \
summary of all the observations in that group.

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
5. Return every establishment you were given, all of them, in the order they \
were given. Keep each name heading, address, inspection type and group label \
exactly as it appears.
6. Return Markdown with the same structure as the input, changing only the \
bullets.
7. Return the Markdown and nothing else — no code fences, no preamble, no \
closing remarks.\
"""

# What the seeded row is called, and what the screen reports when no row exists.
SHIPPED_PROMPT_NAME = "Default"

# Models return fenced text often enough to be worth handling rather than
# pasting ```markdown into a story.
_FENCE = re.compile(r"\A\s*```[a-zA-Z]*\s*\n(.*?)\n?\s*```\s*\Z", re.S)


class Summary:
    """The shortened Markdown, plus what the screen needs to say about it."""

    def __init__(self, markdown, model="", truncated=False, prompt=SHIPPED_PROMPT_NAME,
                 missing=(), unexpected=()):
        self.markdown = markdown
        self.model = model
        self.truncated = truncated
        # Which wording produced this. Without it, comparing two summaries means
        # guessing which prompt made either.
        self.prompt = prompt
        # Establishments sent but not returned, and any the model invented.
        self.missing = list(missing)
        self.unexpected = list(unexpected)

    @property
    def complete(self):
        return not self.missing and not self.unexpected

    def __str__(self):
        return self.markdown


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


def _chunks(blocks, size):
    for start in range(0, len(blocks), size):
        yield blocks[start : start + size]


def _call(payload, token, timeout, session):
    """One request. Returns (text, finish_reason)."""
    try:
        response = (session or requests).post(
            settings.OPENAI_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
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
        body = response.json()
        choice = body["choices"][0]
        text = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise SummaryError("OpenAI's response wasn't in the expected shape.") from exc

    if not (text or "").strip():
        raise SummaryError("OpenAI returned an empty summary.")

    return _unfence(text), choice.get("finish_reason"), body.get("model", "")


def summarize(export, token=None, model=None, timeout=None, session=None, prompt=None):
    """Shorten `export`'s observations and return the whole document.

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
    if not export.blocks:
        raise SummaryError("There is nothing to summarise.")

    if prompt is None:
        from .models import SummaryPrompt

        prompt = SummaryPrompt.active()

    system_message = prompt.system_prompt if prompt else SYSTEM_PROMPT
    prompt_name = prompt.name if prompt else SHIPPED_PROMPT_NAME
    if prompt is None:
        logger.info("No active summary prompt; using the wording shipped in code.")

    chosen_model = model or (prompt.model if prompt else "") or settings.OPENAI_MODEL
    timeout = timeout or settings.OPENAI_TIMEOUT
    batches = list(_chunks(export.blocks, settings.OPENAI_BATCH_SIZE))

    def run(batch):
        document = "\n\n".join(batch)
        payload = {
            "model": chosen_model,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": prompt.render_user_message(document)
                 if prompt else document},
            ],
            # No temperature and no token cap on purpose: which of those two the
            # API accepts changes between model generations, and sending one the
            # chosen model rejects fails the whole call. The defaults suit this.
        }
        return _call(payload, token, timeout, session)

    # Bounded concurrency: a month of a large county is twenty batches, and
    # twenty round trips in series is minutes of someone watching a button.
    # `map` keeps them in order and re-raises the first failure.
    if len(batches) == 1:
        results = [run(batches[0])]
    else:
        with ThreadPoolExecutor(max_workers=settings.OPENAI_MAX_PARALLEL) as pool:
            results = list(pool.map(run, batches))

    returned = "\n\n".join(text for text, _, _ in results)
    answering_model = next((name for _, _, name in results if name), chosen_model)

    sent_names = establishment_names("\n\n".join(export.blocks))
    back_names = establishment_names(returned)
    missing = [name for name in sent_names if name not in back_names]
    unexpected = [name for name in back_names if name not in sent_names]
    if missing or unexpected:
        logger.warning(
            "Summary reconciliation: %s sent, %s returned, missing %s, unexpected %s",
            len(sent_names), len(back_names), missing, unexpected,
        )

    return Summary(
        # The preamble is ours and was never at risk, so it goes back on here.
        f"{export.preamble}\n\n{returned}".strip() + "\n",
        model=answering_model,
        # A batch that stops mid-establishment looks finished unless we say so.
        truncated=any(reason == "length" for _, reason, _ in results),
        prompt=prompt_name,
        missing=missing,
        unexpected=unexpected,
    )
