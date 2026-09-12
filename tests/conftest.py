import os
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

# george.config.settings is a module-level singleton read at import time —
# these must be set before any `george.*` module is imported anywhere in the
# test session. Values are placeholders; no test in this suite makes a real
# network call to Cosmos, Telegram, Anthropic, or Groq.
os.environ.setdefault("COSMOS_ENDPOINT", "https://localhost:8081")
os.environ.setdefault("COSMOS_DATABASE", "george-test")
os.environ.setdefault("COSMOS_KEY", "test-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_WEBHOOK_PATH", "test-path")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("GROQ_API_KEY", "test-groq-key")
os.environ.setdefault("PLATFORM_ADMIN_CHAT_ID", "111111111")
os.environ.setdefault("DRY_RUN", "true")
