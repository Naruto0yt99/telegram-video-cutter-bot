"""
Library navigation module - text and deep-link based browsing.
No fingerprints or indexes needed.
"""

from database import (
    get_animes,
    get_seasons,
    get_episodes,
    get_qualities,
    get_source_url,
    get_all_sources_for_episode,
    add_source,
    edit_source,
    delete_source,
)


def format_anime_list(animes: list) -> str:
    """Format list of anime for Telegram."""
    if not animes:
        return "📚 No anime in library yet."
    
    lines = ["📚 ANIME LIBRARY\n"]
    for i, anime in enumerate(animes, 1):
        lines.append(f"{i}. {anime}")
    return "\n".join(lines)


def format_season_list(anime: str, seasons: list) -> str:
    """Format seasons for an anime."""
    if not seasons:
        return f"❌ No seasons found for {anime}"
    
    lines = [f"📺 {anime} - SEASONS\n"]
    for season in seasons:
        lines.append(f"🎬 Season {season}")
    return "\n".join(lines)


def format_episode_list(anime: str, season: str, episodes: list) -> str:
    """Format episodes for a season."""
    if not episodes:
        return f"❌ No episodes found for {anime} S{season}"
    
    lines = [f"🎬 {anime} Season {season} - EPISODES\n"]
    for ep in episodes:
        lines.append(f"🎞 Episode {ep}")
    return "\n".join(lines)


def format_quality_list(anime: str, season: str, episode: str, qualities: list) -> str:
    """Format quality options for an episode."""
    if not qualities:
        return f"❌ No sources for {anime} S{season}E{episode}"
    
    lines = [f"🎞 {anime} S{season}E{episode} - QUALITIES\n"]
    for quality in qualities:
        lines.append(f"📹 {quality}")
    return "\n".join(lines)


def format_source_links(anime: str, season: str, episode: str, sources: dict) -> str:
    """Format source links for download/display."""
    if not sources:
        return f"❌ No sources for {anime} S{season}E{episode}"
    
    lines = [f"🎞 {anime} S{season}E{episode} - SOURCES\n"]
    for quality, url in sources.items():
        lines.append(f"📹 {quality}: {url}")
    return "\n".join(lines)


def save_episode_sources(
    anime: str,
    season: str,
    num_seasons: int,
    quality_sources: dict,
) -> dict:
    """
    Save batch episode sources.
    
    quality_sources = {
        "480p": ["url1", "url2", ...],  # one URL per episode
        "720p": ["url1", "url2", ...],
        "1080p": ["url1", "url2", ...],
    }
    """
    results = {"success": 0, "failed": 0, "errors": []}
    
    # Determine how many episodes per season
    # Rough estimate: assume episodes are distributed across seasons
    total_sources = len(next(iter(quality_sources.values())))
    episodes_per_season = total_sources // int(num_seasons)
    
    episode_idx = 1
    for season_num in range(1, int(num_seasons) + 1):
        for quality, urls in quality_sources.items():
            for _ in range(episodes_per_season):
                if episode_idx > len(urls):
                    break
                
                try:
                    add_source(
                        anime=anime,
                        season=str(season_num),
                        episode=str(episode_idx % (episodes_per_season + 1) or 1),
                        quality=quality,
                        source_url=urls[episode_idx - 1],
                    )
                    results["success"] += 1
                except Exception as e:
                    results["failed"] += 1
                    results["errors"].append(f"S{season_num}E{episode_idx} {quality}: {e}")
                
                episode_idx += 1
    
    return results


def edit_episode_source(
    anime: str,
    season: str,
    episode: str,
    quality: str,
    new_url: str,
) -> bool:
    """Edit a single source link."""
    try:
        edit_source(anime, season, episode, quality, new_url)
        return True
    except Exception:
        return False


def delete_episode_source(
    anime: str,
    season: str,
    episode: str,
    quality: str,
) -> bool:
    """Delete a source link."""
    try:
        delete_source(anime, season, episode, quality)
        return True
    except Exception:
        return False


# Deep-link helpers for navigation state

class LibraryNavigation:
    """Manage library navigation state for deep-link browsing."""
    
    def __init__(self):
        self.current_anime = None
        self.current_season = None
        self.current_episode = None
    
    def set_anime(self, anime: str):
        self.current_anime = anime
        self.current_season = None
        self.current_episode = None
    
    def set_season(self, season: str):
        self.current_season = season
        self.current_episode = None
    
    def set_episode(self, episode: str):
        self.current_episode = episode
    
    def reset(self):
        self.current_anime = None
        self.current_season = None
        self.current_episode = None
    
    def get_state(self) -> dict:
        return {
            "anime": self.current_anime,
            "season": self.current_season,
            "episode": self.current_episode,
        }
