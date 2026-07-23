"""Application settings loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Central configuration for the promo-bot system."""

    # --- AI API ---
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""  # Custom base URL for Anthropic-compatible APIs (e.g. MiniMax)
    openai_api_key: str = ""
    ai_provider: str = "anthropic"  # "anthropic", "openai", or "template"
    ai_model: str = "claude-sonnet-4-20250514"

    # --- Telegram ---
    tg_bot_token: str = ""
    tg_api_id: int = 0
    tg_api_hash: str = ""

    # --- Database ---
    database_url: str = "postgresql+asyncpg://promo:promo@localhost:5432/promo_bot"
    redis_url: str = "redis://localhost:6379/0"

    # --- Proxy ---
    proxy_provider: str = "brightdata"
    proxy_api_key: str = ""
    proxy_pool_size: int = 60
    local_vpn_host: str = "127.0.0.1"
    local_vpn_port: int = 7890
    local_vpn_protocol: str = "http"  # http or socks5

    # --- Game API ---
    game_api_url: str = "https://api.thebutton.game"
    game_api_key: str = ""
    game_ws_url: str = "wss://ws.thebutton.game"
    game_miniapp_url: str = "https://t.me/rwans_the_button_bot/game"

    # --- Risk Engine Thresholds ---
    risk_threshold_slow: float = 0.4
    risk_threshold_strict: float = 0.5
    risk_threshold_hibernate: float = 0.65
    risk_threshold_abandon: float = 0.8

    # --- Rate Limits ---
    # --- Rate Limits (tuned to avoid Telegram restrictions) ---
    # Strategy: spread activity evenly across 24h, never burst.
    # Join: 3 accounts per round, 15min rounds (每账号各加1个群)
    # Msgs: 每小时每个号选一个群发一条消息 = ~24/day
    # 推广消息不在首次加群时发，一小时后开始
    max_messages_per_day: int = 24
    max_groups_per_day: int = 3  # 每个账号每天最多加3个群（每轮3个账号各加1个）
    max_dms_per_day: int = 0        # disabled — high risk
    max_links_per_day: int = 3      # links trigger anti-spam
    promo_ratio_limit: float = 0.15

    # --- Scheduling ---
    scheduler_interval_seconds: int = 900  # 15 min per round
    nurture_days: int = 45
    infiltration_cooldown_days: int = 14

    # --- Enhanced Discovery Strategies ---
    # Strategy 2: Forward source tracking
    forward_trace_enabled: bool = False
    forward_trace_max_groups: int = 10   # groups to scan per cycle
    forward_trace_max_results: int = 30  # max new groups per cycle

    # Strategy 4: Bio / pinned-message link mining
    bio_links_enabled: bool = False
    bio_links_max_groups: int = 15       # groups to scan per cycle
    bio_links_max_results: int = 30

    # Strategy 1: Member overlap (common chats) — aggressive, use sparingly
    member_overlap_enabled: bool = False
    member_overlap_max_groups: int = 3   # groups per cycle (keep low!)
    member_overlap_max_members: int = 20 # members to sample per group
    member_overlap_max_results: int = 30

    # Strategy 5: Adaptive keyword evolution
    keyword_evolution_enabled: bool = False
    keyword_evolution_max_groups: int = 50  # groups to analyse for keywords
    keyword_evolution_max_keywords: int = 10  # new keywords per cycle

    # Strategy 3: Multi-source web discovery
    web_discovery_enabled: bool = False
    web_discovery_max_results: int = 50
    web_discovery_queries: list[str] = [
        "airdrop", "tap to earn", "gamefi", "web3 game",
        "crypto game", "play to earn", "ton game", "clicker game",
        "mini app", "nft game", "crypto earning", "telegram game",
    ]

    # --- Coordinated Chat ---
    coordinated_chat_enabled: bool = True
    coordinated_chat_chance: float = 0.2  # 20% chance per eligible send_message
    coordinated_chat_min_interval_minutes: int = 3
    coordinated_chat_max_interval_minutes: int = 10

    # --- General ---
    log_level: str = "INFO"
    environment: str = "development"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
