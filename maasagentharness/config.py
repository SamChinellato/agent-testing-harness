import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Endpoint configuration (loaded from .env or environment variables)
    maas_endpoint: str = ""
    maas_host: str = ""
    model_path: str = ""
    model_name: str = "granite-3.1-2b-instruct"
    maas_api_key: str = ""
    user_id: str = "user-a"
    team_id: str = "team-alpha"
    dashboard_url: str = "http://localhost:8000"

    # Agent identity (shown in the dashboard UI)
    display_name: str = ""        # e.g. "Dispute Triage Bot"
    task_description: str = ""    # e.g. "Processing dispute letters"
    agent_type: str = "triage"    # triage | assistant | research
    agent_mode: str = "naive"     # naive | paced | runaway

    # Pacing defaults
    limit_tpm: int = 2000
    paced_target_ratio: float = 0.90  # 90% of limit target

    def get_base_url(self) -> str:
        """Resolve the final base URL ending in /v1."""
        if self.maas_endpoint:
            url = self.maas_endpoint.strip().rstrip("/")
            # Strip trailing /chat/completions or /chat if passed by user
            if url.endswith("/chat/completions"):
                url = url[:-len("/chat/completions")]
            elif url.endswith("/chat"):
                url = url[:-len("/chat")]
            
            if not url.startswith("http://") and not url.startswith("https://"):
                url = f"https://{url}"
            if not url.endswith("/v1"):
                url = f"{url}/v1"
            return url
        
        if self.maas_host:
            host = self.maas_host.strip().rstrip("/")
            model_p = f"/{self.model_path.strip('/')}" if self.model_path.strip() else ""
            if host.startswith("http://") or host.startswith("https://"):
                base = f"{host}{model_p}"
            else:
                base = f"https://{host}{model_p}"
            if not base.endswith("/v1"):
                base = f"{base.rstrip('/')}/v1"
            return base

        raise ValueError("Neither MAAS_ENDPOINT nor MAAS_HOST is configured. Please provide them in your .env file or environment.")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
