import logging

import aiohttp

log = logging.getLogger("skr_bot.supabase")


class SupabaseClient:
    """Client REST minimal pour Supabase (PostgREST).

    Utilise la clé "service_role" (jamais la clé "anon") : elle bypasse la
    Row Level Security, donc elle ne doit exister que côté serveur (variable
    d'environnement), jamais exposée côté client.
    """

    def __init__(self, url: str, service_key: str, table: str = "skr_accouts"):
        self.base_url = url.rstrip("/")
        self.service_key = service_key
        self.table = table
        self._session: aiohttp.ClientSession | None = None

    def _headers(self, prefer: str | None = None) -> dict:
        headers = {
            "apikey": self.service_key,
            "Authorization": f"Bearer {self.service_key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    async def upsert_link(self, record: dict) -> bool:
        """Crée ou met à jour l'entrée d'un membre (basé sur discord_user_id)."""
        session = await self._get_session()
        url = f"{self.base_url}/rest/v1/{self.table}?on_conflict=discord_user_id"
        try:
            async with session.post(
                url,
                json=record,
                headers=self._headers(prefer="resolution=merge-duplicates"),
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    log.error("Échec upsert Supabase (%s) : %s", resp.status, body)
                    return False
                return True
        except aiohttp.ClientError as exc:
            log.exception("Erreur réseau Supabase (upsert) : %s", exc)
            return False

    async def get_by_discord_id(self, discord_user_id: str) -> dict | None:
        """Recherche exacte par ID Discord. Retourne l'entrée ou None."""
        session = await self._get_session()
        params = {
            "discord_user_id": f"eq.{discord_user_id}",
            "select": "*",
        }
        try:
            async with session.get(
                f"{self.base_url}/rest/v1/{self.table}",
                params=params,
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.error("Échec recherche Supabase (%s) : %s", resp.status, body)
                    return None
                data = await resp.json()
                return data[0] if data else None
        except aiohttp.ClientError as exc:
            log.exception("Erreur réseau Supabase (find) : %s", exc)
            return None

    async def delete(self, discord_user_id: str) -> bool:
        session = await self._get_session()
        params = {"discord_user_id": f"eq.{discord_user_id}"}
        try:
            async with session.delete(
                f"{self.base_url}/rest/v1/{self.table}",
                params=params,
                headers=self._headers(prefer="return=representation"),
            ) as resp:
                if resp.status not in (200, 204):
                    body = await resp.text()
                    log.error("Échec suppression Supabase (%s) : %s", resp.status, body)
                    return False
                if resp.status == 204:
                    return True
                data = await resp.json()
                return bool(data)
        except aiohttp.ClientError as exc:
            log.exception("Erreur réseau Supabase (delete) : %s", exc)
            return False