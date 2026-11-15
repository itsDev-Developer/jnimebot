"""Centralized configuration."""

import os
from dataclasses import dataclass, field


def _int_env(name: str, default: int = 0) -> int:
    """Read an int env var; empty / invalid values fall back to default."""
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class SiteConfig:
    base_url: str = "https://animedekho.app"
    ajax_url: str = "https://animedekho.app/wp-admin/admin-ajax.php"
    series_path: str = "/series-hindi"
    movies_path: str = "/movie-hindi"
    episode_path: str = "/epi"
    embed_pattern: str = "https://animedekho.app/?trdekho={server}&trid={post_id}&trtype=2"
    category_api: str = "https://animedekho.app/wp-json/wp/v2/categories"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    bypass_cookie: dict = field(default_factory=lambda: {"toronites_server": "vidstream"})
    request_timeout: int = 15
    # Known trdekho server IDs
    server_ids: dict = field(default_factory=lambda: {
        0: "VidStream",
        1: "HydraX",
        2: "SRuby",
        3: "MirrorBot",
        4: "Server 5",
        5: "VidCloud",
        6: "Strmup",
        7: "Omega",
        8: "Vidmoly",
    })
    # Server priority order: VidStream first, then Omega, VidSrc, Vidmoly, etc.
    preferred_servers: list = field(default_factory=lambda: [
        "VidStream", "Omega", "VidSrc", "Vidmoly", "StreamWish", "FileMoon", "VidCloud", "Strmup", "HydraX"
    ])
    # Default quality buttons always shown to user
    default_qualities: list = field(default_factory=lambda: ["480p", "720p", "1080p", "4K"])


@dataclass(frozen=True)
class CacheConfig:
    default_ttl: int = 300
    search_ttl: int = 120
    categories_ttl: int = 3600
    listing_ttl: int = 180
    max_entries: int = 500


@dataclass(frozen=True)
class BotConfig:
    token: str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    api_id: int = field(default_factory=lambda: _int_env("API_ID"))
    api_hash: str = field(default_factory=lambda: os.environ.get("API_HASH", ""))
    owner_id: int = field(default_factory=lambda: _int_env("OWNER_ID"))
    main_channel: int = field(default_factory=lambda: _int_env("MAIN_CHANNEL"))
    # Where autopost tasks upload the video files (private channel recommended).
    # Falls back to MAIN_CHANNEL when empty.
    storage_channel: int = field(default_factory=lambda: _int_env("STORAGE_CHANNEL"))
    log_channel: int = field(default_factory=lambda: _int_env("LOG_CHANNEL"))
    # ── Low-RAM tuning ──
    max_downloads: int = field(default_factory=lambda: _int_env("MAX_DOWNLOADS", 1))
    max_uploads: int = field(default_factory=lambda: _int_env("MAX_UPLOADS", 2))
    m3u8_threads: int = field(default_factory=lambda: _int_env("M3U8_THREADS", 6))
    # How many series can be worked on at once (RAM/bandwidth still capped by
    # max_downloads/max_uploads above — this only controls how many *series*
    # interleave). Raise this once you have more RAM/bandwidth to scale up.
    max_concurrent_tasks: int = field(default_factory=lambda: _int_env("MAX_CONCURRENT_TASKS", 1))
    # How often (seconds) the tracker checks watched series for new episodes.
    track_poll_seconds: int = field(default_factory=lambda: _int_env("TRACK_POLL_SECONDS", 1800))
    # Timezone used to interpret schedule ("at=") times typed by the owner.
    timezone: str = field(default_factory=lambda: os.environ.get("TIMEZONE", "UTC"))
    # Port for the /health HTTP endpoint (hosting platforms probe this).
    health_port: int = field(default_factory=lambda: _int_env("PORT", 8080))
    # Label shown on the main-channel notification, e.g. "Download In Hindi".
    # Editable at runtime via /settings (stored in DB); this is just the default.
    audio_label: str = field(default_factory=lambda: os.environ.get("AUDIO_LABEL", "Hindi"))
    # Which episode-notification design to use by default; /settings or
    # /settemplate can change this at runtime. One of templates.TEMPLATES or "random".
    notify_template: str = field(default_factory=lambda: os.environ.get("NOTIFY_TEMPLATE", "boxed"))
    mongo_uri: str = field(default_factory=lambda: os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
    items_per_page: int = 10
    max_search_results: int = 15
    max_genres: int = 20
    episodes_per_row: int = 5
    seasons_per_row: int = 4
    slug_max_len: int = 55
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO"))


@dataclass(frozen=True)
class AIConfig:
    api_key: str = field(default_factory=lambda: os.environ.get("AI_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.environ.get("AI_BASE_URL", "https://api.openai.com/v1"))
    model: str = field(default_factory=lambda: os.environ.get("AI_MODEL", "gpt-4o"))
    enabled: bool = field(default_factory=lambda: os.environ.get("AI_ENABLED", "true").lower() in ("true", "1", "yes"))


@dataclass(frozen=True)
class Settings:
    site: SiteConfig = field(default_factory=SiteConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    ai: AIConfig = field(default_factory=AIConfig)


settings = Settings()
