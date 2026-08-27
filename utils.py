import base64
import hashlib
import secrets
import logging

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