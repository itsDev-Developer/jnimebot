"""Main-channel "episode added" notification designs.

Each post announces ONE newly-added episode (or a movie) with a poster and a
single "Download Now" button — the actual file + quality picker lives on the
second (download) channel; these templates never list qualities themselves.

Add a new design = write `def t_xxx(d: NoticeData) -> str` and register it in
TEMPLATES. Only <b> <i> <u> <code> tags are used, plus literal Unicode bold
glyphs where a design wants bold text outside those tags (Telegram photo
captions render HTML fine, but some styles use Unicode bold for flourish).
"""

from __future__ import annotations

import html
import random
from dataclasses import dataclass, field

CAPTION_LIMIT = 1024
DEFAULT_TEMPLATE = "boxed"


@dataclass
class NoticeData:
    title: str
    is_movie: bool = False
    season: int = 1
    episode: int = 1
    audio: str = "Hindi"
    bot_username: str = ""
    genres: list[str] = field(default_factory=list)


# -- helpers --------------------------------------------------------------

def _e(text: str) -> str:
    return html.escape(text or "")


def bold_unicode(text: str) -> str:
    """ASCII -> Mathematical Sans-Serif Bold Unicode glyphs."""
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(0x1D5D4 + (ord(ch) - ord("A"))))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D5EE + (ord(ch) - ord("a"))))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7EC + (ord(ch) - ord("0"))))
        else:
            out.append(ch)
    return "".join(out)


def _episode_line(d: NoticeData) -> str:
    return "Movie Added" if d.is_movie else f"Episode {d.episode:02d} Added"


def _season_tag(d: NoticeData) -> str:
    return "" if d.is_movie else f" (S-{d.season})"


# -- templates --------------------------------------------------------------

def t_boxed(d: NoticeData) -> str:
    """The flagship design: box header + bold Unicode audio badge."""
    badge = bold_unicode(f"Download In {d.audio}")
    return (
        f"\u27b2 <b>{_e(d.title)}</b>{_season_tag(d)}\n\n"
        f"\u250f\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2513\n"
        f"    \U0001f98b  {badge}  \U0001f98b\n"
        f"\u2517\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u251b\n\n"
        f"\u26a1 {_episode_line(d)} \u2714\ufe0f"
    )


def t_minimal(d: NoticeData) -> str:
    return (
        f"<b>{_e(d.title)}</b>{_season_tag(d)}\n\n"
        f"\U0001f195 {_episode_line(d)}\n"
        f"\U0001f50a {_e(d.audio)} Audio"
    )


def t_cinematic(d: NoticeData) -> str:
    lines = ["\u2501" * 20, f"\U0001f3ac <b>{_e(d.title.upper())}</b>{_season_tag(d)}", "\u2501" * 20]
    if d.genres:
        lines.append(f"\U0001f3ad {' \u2022 '.join(_e(g) for g in d.genres[:3])}")
    lines += [
        "",
        f"\u2728 <b>{_e(_episode_line(d))}</b>",
        f"\U0001f50a Audio: {_e(d.audio)}",
        "\u2501" * 20,
    ]
    return "\n".join(lines)


def t_bold_box(d: NoticeData) -> str:
    """All-Unicode-bold title, double box."""
    title_bold = bold_unicode(d.title)
    return (
        f"\u2554{'\u2550' * 18}\u2557\n"
        f"   {title_bold}{_season_tag(d)}\n"
        f"\u255a{'\u2550' * 18}\u255d\n\n"
        f"\U0001f3a7 {bold_unicode(d.audio + ' Audio')}\n"
        f"\u26a1 {_episode_line(d)} \u2714\ufe0f"
    )


def t_hashtag(d: NoticeData) -> str:
    tag = "#" + "".join(ch for ch in d.title if ch.isalnum() or ch == " ").strip().replace(" ", "_")
    audio_tag = "#" + "".join(ch for ch in d.audio if ch.isalnum())
    return (
        f"\U0001f525 <b>{_e(d.title)}</b>{_season_tag(d)}\n"
        f"\U0001f4cc {_episode_line(d)}\n"
        f"\U0001f50a {_e(d.audio)} Audio\n\n"
        f"{tag} {audio_tag}Audio"
    )


def t_neon(d: NoticeData) -> str:
    return (
        f"\u2728\U0001f525 <b>{_e(d.title.upper())}</b> \U0001f525\u2728{_season_tag(d)}\n"
        f"{'\u25ac' * 15}\n"
        f"\u26a1 {_episode_line(d)}\n"
        f"\U0001f3a7 {_e(d.audio)} Audio\n"
        f"{'\u25ac' * 15}\n"
        f"\U0001f447 Tap below to download \U0001f447"
    )


def t_editorial(d: NoticeData) -> str:
    return (
        f"<b>{_e(d.title.upper())}</b>{_season_tag(d)}\n"
        f"<i>NOW AVAILABLE</i>\n\n"
        f"{_episode_line(d)} \u2014 {_e(d.audio)} Audio\n"
        f"{'\u2500' * 24}"
    )


TEMPLATES: dict[str, tuple[str, callable]] = {
    "boxed": ("Boxed (default)", t_boxed),
    "minimal": ("Minimal", t_minimal),
    "cinematic": ("Cinematic", t_cinematic),
    "bold_box": ("Bold Box", t_bold_box),
    "hashtag": ("Hashtag", t_hashtag),
    "neon": ("Neon", t_neon),
    "editorial": ("Editorial", t_editorial),
}
RANDOM = "random"


def template_names() -> list[str]:
    return list(TEMPLATES)


def resolve_name(name: str | None) -> str:
    if name == RANDOM:
        return random.choice(list(TEMPLATES))
    return name if name in TEMPLATES else DEFAULT_TEMPLATE


def render(name: str, data: NoticeData) -> str:
    fn = TEMPLATES.get(name, TEMPLATES[DEFAULT_TEMPLATE])[1]
    return fn(data)[:CAPTION_LIMIT]


SAMPLE = NoticeData(title="Mushoku Tensei : Jobless Reincarnation", season=3, episode=5, audio="Hindi")
