import base64
import hashlib
import secrets


def generate_pkce_pair() -> tuple[str, str]:
    """Retourne (code_verifier, code_challenge) pour PKCE (méthode S256)."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


def sanitise_name(raw_name: str) -> str:
    def format_part(part: str) -> str:
        if "-" in part:
            return "-".join(
                section[:1].upper() + section[1:].lower()
                for section in part.split("-")
            )
        return part[:1].upper() + part[1:].lower()

    return " ".join(format_part(part) for part in raw_name.split(" ") if part)
