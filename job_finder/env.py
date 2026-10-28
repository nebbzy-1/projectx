from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex


ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
_VARIABLE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_env_file() -> dict[str, str]:
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _VARIABLE.match(line)
        if not match:
            continue
        name = match.group(1)
        raw = line[match.end():].strip()
        if not raw:
            values[name] = ""
        elif raw.startswith('"'):
            try:
                values[name] = json.loads(raw.split(" #", 1)[0])
            except (json.JSONDecodeError, TypeError):
                values[name] = raw.strip('"')
        elif raw.startswith("'"):
            try:
                parsed = shlex.split(raw, comments=True)
                values[name] = parsed[0] if parsed else ""
            except ValueError:
                values[name] = raw.strip("'")
        else:
            values[name] = raw.split(" #", 1)[0].strip()
    return values


def load_env() -> None:
    for name, value in read_env_file().items():
        os.environ.setdefault(name, value)


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def write_env_values(values: dict[str, str]) -> None:
    """Update selected .env entries, preserving other settings and comments."""
    if not values:
        return
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []

    existing: set[str] = set()
    output: list[str] = []
    for line in lines:
        match = _VARIABLE.match(line)
        name = match.group(1) if match else ""
        if name in values:
            output.append(f"{name}={json.dumps(str(values[name]), ensure_ascii=False)}")
            existing.add(name)
        else:
            output.append(line)
    for name, value in values.items():
        if name in existing:
            continue
        output.append(f"{name}={json.dumps(str(value), ensure_ascii=False)}")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = ENV_PATH.with_name(".env.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, ENV_PATH)
    for name, value in values.items():
        os.environ[name] = str(value)


def migrate_google_client_file(source: Path) -> bool:
    """Move a Google OAuth client JSON into .env without printing credentials."""
    if not source.is_file():
        return False
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(document, dict):
        return False
    client = document.get("installed") or document.get("web")
    if not isinstance(client, dict):
        return False
    client_id = str(client.get("client_id", "")).strip()
    client_secret = str(client.get("client_secret", "")).strip()
    if not client_id or not client_secret:
        return False

    current = read_env_file()
    fields = {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
        "GOOGLE_PROJECT_ID": str(client.get("project_id", "")).strip(),
        "GOOGLE_AUTH_URI": str(client.get("auth_uri", "")).strip(),
        "GOOGLE_TOKEN_URI": str(client.get("token_uri", "")).strip(),
        "GOOGLE_AUTH_PROVIDER_X509_CERT_URL": str(
            client.get("auth_provider_x509_cert_url", "")
        ).strip(),
        "GOOGLE_REDIRECT_URIS": ",".join(
            str(uri).strip() for uri in client.get("redirect_uris", []) if str(uri).strip()
        ),
    }
    updates = {name: value for name, value in fields.items() if value and not current.get(name)}
    if updates:
        write_env_values(updates)
    saved = read_env_file()
    if any(saved.get(name) != value for name, value in fields.items() if value):
        return False
    source.unlink()
    return True


load_env()
