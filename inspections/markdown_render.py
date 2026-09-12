"""Markdown to HTML, for the web copy and for the editor's preview.

One function, used by both, so what the preview shows is by construction what
the Copy HTML button hands over — a preview that can drift from the real output
is worse than no preview.

Raw HTML passthrough is off. By the time markdown reaches here a person has been
editing it in a textarea, and a stray `<script>` pasted in from who-knows-where
should land in the story as visible text, not as markup. It also keeps the shape
of the output predictable, which matters when it is going into someone's CMS.
"""

from markdown_it import MarkdownIt

# `commonmark` rather than the looser default: a fixed grammar means the HTML a
# given document produces today is the HTML it produces next year.
_renderer = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})


def markdown_to_html(markdown):
    """Render `markdown` to an HTML fragment — no wrapper, no styling."""
    return _renderer.render(markdown or "").strip()
