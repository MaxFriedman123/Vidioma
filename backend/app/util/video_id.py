"""YouTube video-id parsing.

Pure string work, no app state, so it lives outside the main module. Moved
verbatim from app.py; the regexes and precedence are unchanged.
"""
import re

# Utility function to extract video ID from various YouTube URL formats
# Handles watch?v=, youtu.be/, /embed/, /v/, /shorts/, and bare 11-char IDs,
# plus trailing query/fragment params (?t=30, &feature=...).
_YOUTUBE_ID_RE = re.compile(
    r'(?:youtu\.be/|/embed/|/v/|/shorts/|watch\?v=|[?&]v=)([0-9A-Za-z_-]{11})'
)
_BARE_ID_RE = re.compile(r'^[0-9A-Za-z_-]{11}$')


def extract_video_id(url):
    if not isinstance(url, str):
        return None
    url = url.strip()
    match = _YOUTUBE_ID_RE.search(url)
    if match:
        return match.group(1)
    # Allow callers to pass a bare 11-character video ID directly.
    if _BARE_ID_RE.match(url):
        return url
    return None
