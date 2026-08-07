"""Application settings, loaded from the environment / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Empty -> embedded pgserver dev database in ./.pgdata
    database_url: str = ""
    log_level: str = "INFO"
    odds_api_key: str = ""

    # Private ESPN leagues need the browser's session cookies. Optional: without
    # them ESPN reads still work for public leagues, and the draft-time path is
    # unaffected because the extension uses the browser's own session.
    espn_s2: str = ""
    espn_swid: str = ""

    sleeper_base_url: str = "https://api.sleeper.app"
    espn_base_url: str = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"


@lru_cache
def get_settings() -> Settings:
    return Settings()
