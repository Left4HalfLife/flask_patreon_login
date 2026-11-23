"""Generate local bootstrap credentials without third-party dependencies."""

import base64
import os
import secrets
from pathlib import Path
from urllib.parse import quote, urlencode


def main():
    root = Path(__file__).resolve().parent.parent
    password = secrets.token_urlsafe(24)
    totp = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    template = (root / ".env.example").read_text()
    content = (
        template.replace("SECRET_KEY=\n", f"SECRET_KEY={secrets.token_urlsafe(48)}\n")
        .replace("ADMIN_PASSWORD=\n", f"ADMIN_PASSWORD={password}\n")
        .replace("ADMIN_TOTP_SECRET=\n", f"ADMIN_TOTP_SECRET={totp}\n")
    )
    try:
        descriptor = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(".env already exists; refusing to overwrite credentials.")
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
    uri = (
        "otpauth://totp/"
        + quote("Members Studio:admin", safe="")
        + "?"
        + urlencode({"secret": totp, "issuer": "Members Studio"})
    )
    print("Created private .env. Keep these credentials private.")
    print(f"Bootstrap username: admin\nBootstrap password: {password}")
    print(f"Authenticator setup key (TOTP, 6 digits, 30 seconds): {totp}")
    print(f"Authenticator import URI: {uri}")
    print("Enroll the key in your authenticator. Then: docker compose up --build -d")
    print("Open http://localhost:8000/settings and replace the bootstrap password.")


if __name__ == "__main__":
    main()
