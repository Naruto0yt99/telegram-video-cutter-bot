import re
from difflib import get_close_matches


SEASON_RE = re.compile(
    r"\b(?:s|season|session|seasson|seaaon)\s*[-:]?\s*(\d+)\b",
    re.IGNORECASE,
)

EPISODE_RE = re.compile(
    r"\b(?:e|ep|episode)\s*[-:]?\s*(\d+)\b",
    re.IGNORECASE,
)

TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}(?::\d{1,2}){0,2})"
    r"\s*[-–—]\s*"
    r"(?P<end>\d{1,2}(?::\d{1,2}){0,2})"
)


def normalize_space(text: str) -> str:
    return " ".join((text or "").split()).strip()


def extract_season(text: str):
    match = SEASON_RE.search(text)

    if not match:
        return None, text

    season = match.group(1)

    remaining = (
        text[:match.start()] +
        " " +
        text[match.end():]
    )

    return season, normalize_space(remaining)


def extract_episode(text: str):
    match = EPISODE_RE.search(text)

    if not match:
        return None, text

    episode = match.group(1)

    remaining = (
        text[:match.start()] +
        " " +
        text[match.end():]
    )

    return episode, normalize_space(remaining)


def parse_search_query(text: str) -> dict:
    text = normalize_space(text)

    episode, text = extract_episode(text)
    season, text = extract_season(text)

    # Also support compact S2E5.
    compact = re.search(
        r"\bS(\d+)\s*E(\d+)\b",
        text,
        re.IGNORECASE,
    )

    if compact:
        season = season or compact.group(1)
        episode = episode or compact.group(2)

        text = (
            text[:compact.start()] +
            " " +
            text[compact.end():]
        )

    anime = normalize_space(text)

    return {
        "anime": anime,
        "season": season,
        "episode": episode,
    }


def parse_clip_request(text: str):
    """
    Parses:

        Naruto S2 E5 05:00 - 06:30

    Returns:
        anime, season, episode, start, end
    """

    text = normalize_space(text)

    time_match = TIME_RANGE_RE.search(text)

    if not time_match:
        return None

    start = time_match.group("start")
    end = time_match.group("end")

    before_times = normalize_space(
        text[:time_match.start()]
    )

    query = parse_search_query(before_times)

    if not query["anime"]:
        return None

    if not query["season"] or not query["episode"]:
        return None

    return {
        "anime": query["anime"],
        "season": query["season"],
        "episode": query["episode"],
        "start": start,
        "end": end,
    }


def parse_multiple_clip_requests(text: str):
    results = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        parsed = parse_clip_request(line)

        if parsed:
            results.append(parsed)

    return results


def find_closest_name(value: str, choices, cutoff=0.78):
    if not choices:
        return None

    value = normalize_space(value).lower()

    mapping = {
        str(choice).lower(): choice
        for choice in choices
    }

    if value in mapping:
        return mapping[value]

    matches = get_close_matches(
        value,
        mapping.keys(),
        n=1,
        cutoff=cutoff,
    )

    if not matches:
        return None

    return mapping[matches[0]]


def season_label(season) -> str:
    if season is None:
        return ""

    return f"S{season}"


def episode_label(episode) -> str:
    if episode is None:
        return ""

    return f"E{episode}"
