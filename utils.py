import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time

from supabase import acreate_client, AsyncClient

from config import SUPABASE_TABLE

log = logging.getLogger("skr_bot.supabase")

# Longueur fixe du code_verifier PKCE : 32 octets aléatoires -> exactement
# 43 caractères en base64url sans padding (minimum autorisé par la RFC 7636,
# choisi ici sciemment pour garder le state le plus court possible — voir
# plus bas pourquoi la taille compte).
CODE_VERIFIER_BYTES = 32
CODE_VERIFIER_LEN = 43


def generate_pkce_pair() -> tuple[str, str]:
    """Retourne (code_verifier, code_challenge) pour PKCE (méthode S256)."""
    code_verifier = secrets.token_urlsafe(CODE_VERIFIER_BYTES)
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
# partagée (HMAC tronqué) pour empêcher qu'un utilisateur les falsifie, et
# horodatées pour qu'elles expirent après un délai (voir LOGIN_TIMEOUT_SECONDS).
#
# `state` finit dans l'URL d'un bouton Discord ("Se connecter à vAMSYS"),
# et Discord limite l'URL d'un bouton à 512 caractères. Un encodage JSON
# classique dépasse largement cette limite une fois combiné au reste de
# l'URL d'autorisation (redirect_uri, scope, code_challenge...). On utilise
# donc un format binaire compact à taille fixe plutôt que du JSON :
#   [code_verifier: 43 octets ASCII][discord_user_id: 8 octets][guild_id: 8
#   octets][created_at: 4 octets][signature HMAC-SHA256 tronquée: 16 octets]
# soit 79 octets bruts -> 106 caractères une fois encodés en base64url,
# contre 300+ avec la version JSON. 128 bits de signature restent largement
# suffisants pour empêcher toute falsification.
#
# ⚠️ Ce module a une copie jumelle côté fonction Vercel
# (vercel/api/index.py, section "Jeton d'état signé"). Les deux copies
# doivent produire/lire des jetons strictement compatibles : si tu modifies
# le format ici, répercute le changement là-bas.

_PAYLOAD_STRUCT = ">QQI"  # discord_user_id (8o), guild_id (8o), created_at (4o)
_PAYLOAD_LEN = CODE_VERIFIER_LEN + struct.calcsize(_PAYLOAD_STRUCT)
_SIGNATURE_LEN = 16  # HMAC-SHA256 tronqué à 128 bits


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_state_token(code_verifier: str, discord_user_id: int, guild_id: int, secret: str) -> str:
    """Encode (code_verifier, discord_user_id, guild_id, horodatage) en un
    jeton signé compact, utilisable comme paramètre `state` OAuth."""
    verifier_bytes = code_verifier.encode("ascii")
    if len(verifier_bytes) != CODE_VERIFIER_LEN:
        raise ValueError(f"code_verifier doit faire {CODE_VERIFIER_LEN} caractères")

    payload = verifier_bytes + struct.pack(_PAYLOAD_STRUCT, discord_user_id, guild_id, int(time.time()))
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:_SIGNATURE_LEN]
    return _b64url_encode(payload + signature)


def verify_state_token(token: str, secret: str, max_age_seconds: int) -> dict | None:
    """Vérifie la signature et l'expiration d'un jeton `state`.

    Retourne {"code_verifier", "discord_user_id", "guild_id", "created_at"}
    si le jeton est valide et non expiré, sinon None (jeton corrompu,
    signature invalide ou expiré)."""
    try:
        raw = _b64url_decode(token)
    except Exception:
        return None

    if len(raw) != _PAYLOAD_LEN + _SIGNATURE_LEN:
        return None

    payload, signature = raw[:_PAYLOAD_LEN], raw[_PAYLOAD_LEN:]

    expected_signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()[:_SIGNATURE_LEN]
    if not hmac.compare_digest(expected_signature, signature):
        return None

    try:
        code_verifier = payload[:CODE_VERIFIER_LEN].decode("ascii")
        discord_user_id, guild_id, created_at = struct.unpack(_PAYLOAD_STRUCT, payload[CODE_VERIFIER_LEN:])
    except Exception:
        return None

    if time.time() - created_at > max_age_seconds:
        return None

    return {
        "code_verifier": code_verifier,
        "discord_user_id": discord_user_id,
        "guild_id": guild_id,
        "created_at": created_at,
    }


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