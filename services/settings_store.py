import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env.local"

SECRET_KEYS = {
    "GMAIL_ADDRESS",
    "GMAIL_APP_PASSWORD",
    "EMAIL_PROVIDER",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_SECURITY",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SENDER_EMAIL",
    "FROM_NAME",
    "REPLY_TO",
    "SEARCH_PROVIDER",
    "BRAVE_API_KEY",
    "SENDER_COMPANY",
    "SENDER_PHONE",
    "SENDER_WEBSITE",
    "MONTHLY_CAPACITY",
    "MOQ",
    "PRICE",
    "INCOTERM",
}

DEFAULTS = {
    "GMAIL_ADDRESS": "",
    "GMAIL_APP_PASSWORD": "",
    "EMAIL_PROVIDER": "gmail",
    "SMTP_HOST": "",
    "SMTP_PORT": "465",
    "SMTP_SECURITY": "ssl",
    "SMTP_USERNAME": "",
    "SMTP_PASSWORD": "",
    "SENDER_EMAIL": "",
    "FROM_NAME": "Export Team",
    "REPLY_TO": "",
    "SEARCH_PROVIDER": "ddgs",
    "BRAVE_API_KEY": "",
    "SENDER_COMPANY": "Your Company",
    "SENDER_PHONE": "",
    "SENDER_WEBSITE": "",
    "MONTHLY_CAPACITY": "Available on request",
    "MOQ": "1 pallet / container on request",
    "PRICE": "Available on request",
    "INCOTERM": "FCA Armenia / CIF on request",
}


def load_env_file():
    values = DEFAULTS.copy()
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in SECRET_KEYS:
                values[key] = value.replace("\\n", "\n")
    for key in SECRET_KEYS:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def save_env(values):
    current = load_env_file()
    keep_if_blank = {"GMAIL_APP_PASSWORD", "SMTP_PASSWORD", "BRAVE_API_KEY"}
    for key, value in values.items():
        if key in SECRET_KEYS and value is not None:
            if key in keep_if_blank and value == "" and current.get(key):
                continue
            current[key] = str(value).strip()
    lines = ["# Local secrets/settings. Do not commit this file."]
    for key in sorted(SECRET_KEYS):
        value = current.get(key, "").replace("\n", "\\n")
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ENV_PATH.chmod(0o600)
    except OSError:
        pass
    return current


def masked_settings():
    values = load_env_file()
    return {
        **values,
        "GMAIL_APP_PASSWORD_SET": bool(values.get("GMAIL_APP_PASSWORD")),
        "SMTP_PASSWORD_SET": bool(values.get("SMTP_PASSWORD")),
        "BRAVE_API_KEY_SET": bool(values.get("BRAVE_API_KEY")),
        "GMAIL_APP_PASSWORD": "",
        "SMTP_PASSWORD": "",
        "BRAVE_API_KEY": "",
    }
