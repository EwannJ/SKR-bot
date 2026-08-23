import logging
import secrets
import time
from datetime import datetime, timezone

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

import config
from utils import generate_pkce_pair, sanitise_name

log = logging.getLogger("skr_bot.vamsys")


def _compute_team(pilot_data: dict) -> str | None:
    rank = pilot_data.get("rank") or {}
    name: str | None = rank.get("name")
    return name


class VamsysCog(commands.Cog):
    """Gère tout le flux OAuth PKCE vAMSYS : lancement du lien, callback web,
    échange de token, récupération du profil pilote et application du
    pseudo/rôle côté Discord."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # state -> {code_verifier, discord_user_id, guild_id, created_at}
        self.pending_logins: dict[str, dict] = {}
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------
    # Cycle de vie du cog : démarre/arrête le mini-serveur web du callback
    # ------------------------------------------------------------------
    async def cog_load(self) -> None:
        app = web.Application()
        app.router.add_get("/vamsys/callback", self.handle_callback)

        self._runner = web.AppRunner(app)
        await self._runner.setup() # type: ignore
        site = web.TCPSite(self._runner, "0.0.0.0", config.LOCAL_PORT)
        await site.start()
        log.info("Serveur web local démarré sur le port %s", config.LOCAL_PORT)

    async def cog_unload(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # ------------------------------------------------------------------
    # Gestion des liaisons en attente
    # ------------------------------------------------------------------
    def _cleanup_expired_logins(self) -> None:
        now = time.time()
        expired = [
            state
            for state, data in self.pending_logins.items()
            if now - data["created_at"] > config.LOGIN_TIMEOUT_SECONDS
        ]
        for state in expired:
            self.pending_logins.pop(state, None)

    def create_pending_login(self, discord_user_id: int, guild_id: int) -> tuple[str, str]:
        """Crée une tentative de liaison PKCE et retourne (state, code_challenge)."""
        self._cleanup_expired_logins()

        code_verifier, code_challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(32)

        self.pending_logins[state] = {
            "code_verifier": code_verifier,
            "discord_user_id": discord_user_id,
            "guild_id": guild_id,
            "created_at": time.time(),
        }
        return state, code_challenge

    def build_authorize_url(self, state: str, code_challenge: str) -> str:
        params = {
            "client_id": config.VAMSYS_CLIENT_ID,
            "redirect_uri": config.REDIRECT_URI,
            "response_type": "code",
            "scope": config.VAMSYS_SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        query_string = "&".join(
            f"{k}={aiohttp.helpers.quote(v, safe='')}" for k, v in params.items()
        )
        return f"{config.VAMSYS_AUTHORIZE_URL}?{query_string}"

    # ------------------------------------------------------------------
    # Application du pseudo/rôle côté Discord
    # ------------------------------------------------------------------
    async def apply_pilot_to_member(
        self, guild: discord.Guild, member: discord.Member, pilot_data: dict
    ) -> tuple[bool, str]:
        server_config = config.SERVERS.get(str(guild.id))
        if server_config is None:
            return False, "Serveur non configuré."

        first_name = (pilot_data.get("first_name") or pilot_data.get("firstName") or "").strip()
        last_name = (pilot_data.get("last_name") or pilot_data.get("lastName") or "").strip()
        pilot_id = pilot_data.get("username") or pilot_data.get("pilot_id") or ""

        # Formatage du nom de famille : on ne garde que l'initiale suivie d'un point (ex: "J.")
        last_initial = f"{last_name[0].upper()}." if last_name else ""
        
        # Reconstruction du nom complet (ex: "Ewann J.")
        formatted_name = f"{first_name} {last_initial}".strip()
        full_name = sanitise_name(formatted_name)
        
        separator = server_config["nickSeparator"]

        # Si vAMSYS ne renvoie pas de nom, on se rabat sur l'ID de pilote seul
        new_nick = f"{full_name}{separator}{pilot_id}".strip() if full_name else pilot_id

        errors: list[str] = []

        # --- Pseudo : tenté indépendamment du reste ---
        try:
            await member.edit(nick=new_nick)
        except discord.Forbidden:
            errors.append(
                "pseudo non modifié (rôle du bot trop bas, ou membre = propriétaire du serveur)"
            )

        # --- Rôles : toujours tenté ---
        role_removal_cfg = server_config.get("roleRemoval", {"enabled": False, "roleId": []})
        user_role_ids = [r.id for r in member.roles if r.id != guild.id]

        if role_removal_cfg.get("enabled", False):
            to_remove = set(str(r) for r in role_removal_cfg.get("roleId", []))
            user_role_ids = [rid for rid in user_role_ids if str(rid) not in to_remove]

        for role_id in server_config.get("accessRoleId", []):
            rid = int(role_id)
            if rid not in user_role_ids:
                user_role_ids.append(rid)

        try:
            new_roles = [guild.get_role(rid) for rid in user_role_ids]
            new_roles = [r for r in new_roles if r is not None]
            await member.edit(roles=new_roles)
        except discord.Forbidden:
            errors.append("rôles non modifiés (rôle du bot trop bas)")

        if not errors:
            return True, "OK"

        return True, "Partiel : " + " ; ".join(errors)

    async def remove_pilot_from_member(
        self, guild: discord.Guild, member: discord.Member
    ) -> tuple[bool, str]:
        """Retire le(s) rôle(s) d'accès configurés et réinitialise le pseudo
        au pseudo Discord par défaut. Utilisé par /removeaccount."""
        server_config = config.SERVERS.get(str(guild.id))
        if server_config is None:
            return False, "Serveur non configuré."

        errors: list[str] = []

        try:
            await member.edit(nick=None)
        except discord.Forbidden:
            errors.append(
                "pseudo non réinitialisé (rôle du bot trop bas, ou membre = propriétaire du serveur)"
            )

        access_role_ids = {int(rid) for rid in server_config.get("accessRoleId", [])}
        remaining_roles = [r for r in member.roles if r.id != guild.id and r.id not in access_role_ids]

        try:
            await member.edit(roles=remaining_roles)
        except discord.Forbidden:
            errors.append("rôle non retiré (rôle du bot trop bas)")

        if not errors:
            return True, "OK"
        return True, "Partiel : " + " ; ".join(errors)

    # ------------------------------------------------------------------
    # Callback OAuth (route web)
    # ------------------------------------------------------------------
    async def handle_callback(self, request: web.Request) -> web.Response:
        self._cleanup_expired_logins()

        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")

        if error:
            return web.Response(
                text=f"Autorisation refusée ou erreur vAMSYS : {error}. Tu peux fermer cette page.",
                status=400,
            )

        if not code or not state or state not in self.pending_logins:
            return web.Response(
                text="Lien invalide ou expiré. Retourne sur Discord et reclique sur le bouton.",
                status=400,
            )

        login_data = self.pending_logins.pop(state)

        # --- Échange du code contre un token (pas de client_secret : client PKCE public) ---
        async with aiohttp.ClientSession() as session:
            token_payload = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.REDIRECT_URI,
                "client_id": config.VAMSYS_CLIENT_ID,
                "code_verifier": login_data["code_verifier"],
            }

            try:
                async with session.post(config.VAMSYS_TOKEN_URL, data=token_payload) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error("Échec de l'échange de token vAMSYS (%s) : %s", resp.status, body)
                        return web.Response(text="Erreur lors de la connexion à vAMSYS. Réessaie.", status=502)
                    token_data = await resp.json()
            except aiohttp.ClientError as exc:
                log.exception("Erreur réseau lors de l'échange de token : %s", exc)
                return web.Response(text="Erreur réseau, réessaie plus tard.", status=502)

            access_token = token_data.get("access_token")
            if not access_token:
                return web.Response(text="Réponse vAMSYS invalide (pas de token).", status=502)

            # --- Récupération du profil pilote (rang, username) ---
            headers = {"Authorization": f"Bearer {access_token}"}
            try:
                async with session.get(config.VAMSYS_PILOT_ME_URL, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error("Échec de récupération du profil pilote (%s) : %s", resp.status, body)
                        return web.Response(
                            text=(
                                "Impossible de récupérer ton profil vAMSYS.\n\n"
                                f"[DEBUG] URL appelée : {config.VAMSYS_PILOT_ME_URL}\n"
                                f"[DEBUG] Code HTTP : {resp.status}\n"
                                f"[DEBUG] Réponse vAMSYS : {body}"
                            ),
                            status=502,
                        )
                    raw_data = await resp.json()
                    # L'API vAMSYS enveloppe la réponse dans une clé "data"
                    pilot_data = raw_data.get("data", raw_data) if isinstance(raw_data, dict) else raw_data
                    log.info("Profil pilote reçu : %s", pilot_data)

                # --- Récupération de l'identité (nom) — endpoint séparé ---
                async with session.get(config.VAMSYS_USER_URL, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("Échec de récupération de l'identité pilote (%s) : %s", resp.status, body)
                        identity_data = {}
                    else:
                        raw_identity = await resp.json()
                        identity_data = (
                            raw_identity.get("data", raw_identity)
                            if isinstance(raw_identity, dict)
                            else raw_identity
                        )
                        log.info("Identité pilote reçue : %s", identity_data)
            except aiohttp.ClientError as exc:
                log.exception("Erreur réseau lors de la récupération du profil : %s", exc)
                return web.Response(text=f"Erreur réseau, réessaie plus tard.\n\n[DEBUG] {exc}", status=502)

        # --- Fusion des deux réponses : rang depuis /profile, nom depuis /user ---
        first_name = identity_data.get("first_name") or pilot_data.get("first_name") or ""
        last_name = identity_data.get("last_name") or pilot_data.get("last_name") or ""
        skr_id = (
            pilot_data.get("username")
            or (identity_data.get("pilot") or {}).get("username")
            or ""
        )

        guild = self.bot.get_guild(login_data["guild_id"])
        if guild is None:
            return web.Response(text="Le bot ne trouve plus ce serveur Discord.", status=500)

        member = guild.get_member(login_data["discord_user_id"])
        if member is None:
            return web.Response(text="Tu ne sembles plus être membre de ce serveur Discord.", status=400)

        # --- Enregistrement en base D'ABORD : si ça échoue, on n'applique
        # ni le pseudo ni le rôle (source de vérité = la base). ---
        db_ok = await self.bot.supabase.upsert_link(
            {
                "discord_user_id": str(member.id),
                "skr_id": skr_id,
                "first_name": first_name,
                "last_name": last_name,
                "team": _compute_team(pilot_data),
                "linked_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if not db_ok:
            log.error("Échec d'enregistrement Supabase pour %s (%s)", member, skr_id)
            return web.Response(
                text="❌ Erreur lors de l'enregistrement en base de données. "
                "Ton pseudo et ton rôle n'ont pas été modifiés. Réessaie, ou contacte un administrateur.",
                status=500,
            )

        # --- Application du pseudo/rôle côté Discord (seulement si la DB a réussi) ---
        merged_data = {**pilot_data, "first_name": first_name, "last_name": last_name, "username": skr_id}
        success, message = await self.apply_pilot_to_member(guild, member, merged_data)

        if success and message == "OK":
            return web.Response(
                text="✅ Compte lié avec succès ! Ton pseudo et ton rôle ont été mis à jour. "
                "Tu peux fermer cette page et retourner sur Discord."
            )
        elif success:
            log.warning("Liaison partielle pour %s : %s", member, message)
            return web.Response(
                text=f"✅ Compte lié, mais avec un avertissement : {message}\n\n"
                "Tu peux fermer cette page. Contacte un administrateur si besoin."
            )
        else:
            log.error("Échec de l'application du pseudo/rôle pour %s : %s", member, message)
            return web.Response(text=f"Connexion réussie, mais erreur côté Discord : {message}", status=500)


async def setup(bot: commands.Bot):
    await bot.add_cog(VamsysCog(bot))