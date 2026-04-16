"""
Settings manager with encrypted storage for sensitive values like API keys.

Uses Fernet symmetric encryption with a machine-derived key stored alongside
the settings file. Settings are persisted to a local JSON file.
"""

import json
import os
import base64
from pathlib import Path
from cryptography.fernet import Fernet

SETTINGS_DIR = Path(__file__).parent / ".settings"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
KEY_FILE = SETTINGS_DIR / ".key"

DEFAULT_WELCOME_MESSAGE = (
    "Welcome to Skills Crafter! "
    "Once you connect to this server your player data is being captured. "
    "If you do not want this then please disconnect."
)

DEFAULT_SETTINGS = {
    "llm_provider": "",        # "azure", "openai", "anthropic"
    "llm_api_key": "",         # encrypted
    "llm_endpoint": "",        # Azure Foundry endpoint URL (only for Azure)
    "welcome_message": DEFAULT_WELCOME_MESSAGE,
    "welcome_color": "green",  # Minecraft color code name
    "show_trace_paths": True,  # show fading movement trails on the 2D map
    "report_detail_level": 3,  # 1 (minimal) to 5 (comprehensive)
}


def _get_or_create_key():
    """Get or create the encryption key."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    KEY_FILE.write_bytes(key)
    os.chmod(str(KEY_FILE), 0o600)
    return key


def _get_fernet():
    return Fernet(_get_or_create_key())


def _encrypt(value):
    if not value:
        return ""
    return _get_fernet().encrypt(value.encode()).decode()


def _decrypt(value):
    if not value:
        return ""
    try:
        return _get_fernet().decrypt(value.encode()).decode()
    except Exception:
        return ""


def load_settings():
    """Load settings from disk, decrypting sensitive fields."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULT_SETTINGS)
        return dict(DEFAULT_SETTINGS)

    try:
        raw = json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_SETTINGS)

    settings = dict(DEFAULT_SETTINGS)
    settings.update(raw)

    # Decrypt API key
    settings["llm_api_key"] = _decrypt(settings.get("llm_api_key", ""))
    return settings


def save_settings(settings):
    """Save settings to disk, encrypting sensitive fields."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    to_save = dict(settings)

    # Encrypt API key before saving
    if to_save.get("llm_api_key"):
        to_save["llm_api_key"] = _encrypt(to_save["llm_api_key"])

    SETTINGS_FILE.write_text(json.dumps(to_save, indent=2))
    os.chmod(str(SETTINGS_FILE), 0o600)


def mask_api_key(key):
    """Return a masked version of the API key for display."""
    if not key or len(key) < 8:
        return ""
    return key[:4] + "*" * (len(key) - 8) + key[-4:]


# --- Rubric Storage ---
RUBRICS_FILE = SETTINGS_DIR / "rubrics.json"


def load_rubrics():
    """Load rubrics from disk."""
    if not RUBRICS_FILE.exists():
        return []
    try:
        return json.loads(RUBRICS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_rubrics(rubrics):
    """Save rubrics to disk."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    RUBRICS_FILE.write_text(json.dumps(rubrics, indent=2))
