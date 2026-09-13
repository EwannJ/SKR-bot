import logging
import urllib.parse

import discord
from discord.ext import commands

import config
from utils import generate_pkce_pair, sanitise_name, create_state_token

log = logging.getLogger("skr_bot.vamsys")


class VamsysCog(commands.Cog):
    """Gère la partie Discord du flux OAuth PKCE vAMSYS.

    Le cog génère le lien de connexion (create_pending_login /
    build_authorize_url) et sait appliquer le pseudo/rôle une fois un
    compte lié (apply_pilot_to_member) ou le retirer (remove_pilot_from_member).

    Le callback OAuth (échange du code, appels à l'API vAMSYS, écriture en
    base) n'est PLUS traité ici : il est géré par une fonction serverless
    Vercel (vercel/api/callback.py), qui expose une URL publique fixe sans
    qu'on ait besoin d'un serveur web local ni d'un tunnel ngrok. C'est
    aussi pour ça que create_pending_login encode toutes les infos
    nécessaires directement dans `state` (signées), plutôt que dans un dict
    en mémoire : la fonction Vercel tourne dans un process séparé et n'a
    pas accès à la mémoire du bot.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Lancement du flux de liaison
    # ------------------------------------------------------------------
    def create_pending_login(self, discord_user_id: int, guild_id: int) -> tuple[str, str]:
        """Crée une tentative de liaison PKCE et retourne (state, code_challenge)."""
        code_verifier, code_challenge = generate_pkce_pair()

        state = create_state_token(
            code_verifier, discord_user_id, guild_id, config.VAMSYS_STATE_SECRET # type: ignore
        )
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
            f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params.items()
        )
        return f"{config.VAMSYS_AUTHORIZE_URL}?{query_string}"

    # ------------------------------------------------------------------
    # Application du pseudo/rôle côté Discord
    #
    # NOTE : depuis le passage à Vercel, ces deux méthodes ne sont plus
    # appelées pendant le flux de liaison lui-même (la fonction Vercel fait
    # l'équivalent via l'API REST Discord directement — voir
    # apply_pilot_to_member dans vercel/api/callback.py). apply_pilot_to_member
    # reste ici pour /removeaccount (remove_pilot_from_member) et pour un
    # éventuel futur usage (ex: une commande /resync manuelle).
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


async def setup(bot: commands.Bot):
    await bot.add_cog(VamsysCog(bot))