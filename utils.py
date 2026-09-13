import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from supabase import acreate_client, AsyncClient

from config import SUPABASE_TABLE

log = logging.getLogger("skr_bot.supabase")


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


# --- JETON D'ÉTAT SIGNÉ (remplace pending_logins) ---
#
# Avant : le callback OAuth était traité par le bot lui-même (serveur aiohttp
# local + tunnel ngrok), donc `state` pouvait être un simple identifiant
# aléatoire pointant vers un dict en mémoire (`pending_logins`).
#
# Maintenant : le callback est traité par une fonction Vercel, qui tourne
# dans un process complètement séparé et sans état entre deux appels. Il n'y
# a donc plus de mémoire partagée où chercher `code_verifier`,
# `discord_user_id` et `guild_id` à partir de `state`. Solution : on encode
# ces informations directement DANS `state`, signées avec une clé secrète
# partagée (HMAC-SHA256) pour empêcher qu'un utilisateur les falsifie, et
# horodatées pour qu'elles expirent après un délai (voir LOGIN_TIMEOUT_SECONDS).
#
# ⚠️ Ce module a une copie jumelle côté fonction Vercel
# (vercel/api/callback.py, section "Jeton d'état signé"). Les deux copies
# doivent produire des jetons strictement compatibles : si tu modifies la
# logique ici, répercute le changement là-bas.

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_state_token(payload: dict, secret: str) -> str:
    """Encode un payload en jeton signé utilisable comme paramètre `state` OAuth."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body_b64 = _b64url_encode(body)
    signature = hmac.new(secret.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{body_b64}.{_b64url_encode(signature)}"


def verify_state_token(token: str, secret: str, max_age_seconds: int) -> dict | None:
    """Vérifie la signature et l'expiration d'un jeton `state`.

    Retourne le payload d'origine si le jeton est valide et non expiré,
    sinon None (jeton corrompu, signature invalide ou expiré)."""
    try:
        body_b64, signature_b64 = token.split(".", 1)
    except ValueError:
        return None

    expected_signature = hmac.new(secret.encode("utf-8"), body_b64.encode("ascii"), hashlib.sha256).digest()
    try:
        provided_signature = _b64url_decode(signature_b64)
    except Exception:
        return None

    if not hmac.compare_digest(expected_signature, provided_signature):
        return None

    try:
        payload = json.loads(_b64url_decode(body_b64))
    except Exception:
        return None

    created_at = payload.get("created_at")
    if not isinstance(created_at, (int, float)) or time.time() - created_at > max_age_seconds:
        return None

    return payload


# --- SUPABASE ---

class SupabaseClient:
    """Client Supabase basé sur le SDK officiel `supabase-py`.
 
    Utilise la clé "service_role" (jamais la clé "anon") : elle bypasse la
    Row Level Security, donc elle ne doit exister que côté serveur (variable
    d'environnement), jamais exposée côté client.
    """
 
    def __init__(self, client: AsyncClient, table: str = SUPABASE_TABLE):
        self._client = client
        self.table = table
 
    @classmethod
    async def create(cls, url: str, service_key: str, table: str = SUPABASE_TABLE) -> "SupabaseClient":
        """Fabrique asynchrone : le SDK crée le client via une coroutine,
        donc on ne peut pas tout faire dans __init__ (qui est synchrone)."""
        client = await acreate_client(url, service_key)
        return cls(client, table)
 
    async def close(self) -> None:
        """Rien à fermer explicitement : le SDK gère sa session HTTP en
        interne. Méthode conservée pour garder le même appel dans main.py."""
        pass
 
    # ------------------------------------------------------------------
    async def upsert_link(self, record: dict) -> bool:
        """Crée ou met à jour l'entrée d'un membre (basé sur discord_user_id)."""
        try:
            await (
                self._client.table(self.table)
                .upsert(record, on_conflict="discord_user_id")
                .execute()
            )
            return True
        except Exception as exc:
            log.exception("Échec upsert Supabase : %s", exc)
            return False
 
    async def get_by_discord_id(self, discord_user_id: str) -> dict | None:
        """Recherche exacte par ID Discord. Retourne l'entrée ou None."""
        try:
            response = await (
                self._client.table(self.table)
                .select("*")
                .eq("discord_user_id", discord_user_id)
                .execute()
            )
            return response.data[0] if response.data else None
        except Exception as exc:
            log.exception("Échec recherche Supabase : %s", exc)
            return None
 
    async def ping(self) -> bool:
        """Requête minimale servant uniquement à générer de l'activité DB,
        pour empêcher Supabase de mettre le projet en pause (free tier :
        pause après 7 jours sans activité). Ne renvoie aucune donnée."""
        try:
            await (
                self._client.table(self.table)
                .select("discord_user_id")
                .limit(1)
                .execute()
            )
            return True
        except Exception as exc:
            log.exception("Échec du ping anti-pause Supabase : %s", exc)
            return False
 
    async def delete(self, discord_user_id: str) -> bool:
        try:
            await (
                self._client.table(self.table)
                .delete()
                .eq("discord_user_id", discord_user_id)
                .execute()
            )
            return True
        except Exception as exc:
            log.exception("Échec suppression Supabase : %s", exc)
            return False