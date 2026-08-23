import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.vamsys import VamsysCog

log = logging.getLogger("skr_bot.commandes")

# ---------------------------------------------------------------------------
# Tickets exécutifs : reconnaissance des noms de salon
# ---------------------------------------------------------------------------
# ⚪dmd-ID (Demande), 🟢pirep-ID (PIREP), 🔴sign-ID (Signalement)
TICKET_NAME_RE = re.compile(r"^(⚪dmd|🟢pirep|🔴sign)-(.+)$")
EXEC_NAME_RE = re.compile(r"^🟡exec-(.+)$")
ORIGINAL_TOPIC_RE = re.compile(r"^original:(.+)$")


async def _safe_edit_channel(channel: discord.TextChannel, **kwargs) -> str | None:
    """Tente channel.edit(**kwargs). Retourne None si OK, sinon un message
    d'erreur utilisateur (distingue la limite Discord de renommage — 2 fois
    max par salon toutes les 10 minutes — des autres erreurs)."""
    try:
        await channel.edit(**kwargs)
        return None
    except discord.Forbidden:
        return "❌ Le bot n'a pas la permission de modifier ce salon."
    except discord.HTTPException as exc:
        if exc.status == 429 or getattr(exc, "code", None) == 30016:
            return (
                "❌ Limite Discord atteinte : ce salon a déjà été renommé trop de fois "
                "récemment (max 2 renommages par salon toutes les 10 minutes). "
                "Réessaie dans quelques minutes."
            )
        log.exception("Erreur HTTP Discord lors de l'édition du salon : %s", exc)
        return f"❌ Erreur Discord lors de la modification du salon : {exc}"


def _format_linked_at(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:f>"


def _format_record(record: dict, member: discord.Member) -> discord.Embed:
    embed = discord.Embed(title="Compte lié", color=config.EMBED_COLOR)
    embed.add_field(name="ID SKR", value=record.get("skr_id") or "—", inline=False)
    full_name = f"{record.get('first_name') or ''} {record.get('last_name') or ''}".strip()
    embed.add_field(name="Nom", value=full_name or "—", inline=False)
    embed.add_field(name="Discord", value=member.mention, inline=False)
    team = record.get("team")
    embed.add_field(name="Rôle", value=team, inline=False)
    embed.add_field(name="Lié le", value=_format_linked_at(record.get("linked_at")), inline=False)
    return embed


class LinkAccountView(discord.ui.View):
    """Vue persistante avec le bouton 'Lier mon compte vAMSYS'."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        # Sans ça, une erreur inattendue (ex: vAMSYS injoignable, cog non
        # chargé, etc.) fait juste échouer l'interaction côté Discord sans
        # rien logger — impossible à diagnostiquer. On log ET on prévient
        # l'utilisateur au lieu de le laisser avec "Cette interaction a échoué".
        log.exception("Erreur dans le bouton de liaison vAMSYS : %s", error)
        message = (
            "Une erreur inattendue est survenue. Réessaie dans quelques instants, "
            "et préviens un administrateur si ça persiste."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(
        label="Lier mon compte vAMSYS",
        style=discord.ButtonStyle.primary,
        custom_id="link-vamsys-account",
    )
    async def link_account(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.guild_id) not in config.SERVERS:
            await interaction.response.send_message(
                "Ce serveur n'est pas configuré correctement. Contactez un administrateur.",
                ephemeral=True,
            )
            return

        vamsys_cog: VamsysCog = self.bot.get_cog("VamsysCog")
        if vamsys_cog is None:
            await interaction.response.send_message(
                "Le module vAMSYS n'est pas chargé. Contactez un administrateur.",
                ephemeral=True,
            )
            return

        state, code_challenge = vamsys_cog.create_pending_login(
            interaction.user.id, interaction.guild_id
        )
        authorize_url = vamsys_cog.build_authorize_url(state, code_challenge)

        link_view = discord.ui.View()
        link_view.add_item(
            discord.ui.Button(
                label="Se connecter à vAMSYS",
                style=discord.ButtonStyle.link,
                url=authorize_url,
            )
        )

        await interaction.response.send_message(
            "Clique sur le bouton ci-dessous, connecte-toi à vAMSYS et autorise l'accès. "
            "Ton pseudo et ton rôle seront mis à jour automatiquement juste après — "
            "tu n'as rien d'autre à faire ici.",
            view=link_view,
            ephemeral=True,
        )


class CommandesCog(commands.Cog):
    """Slash commands du bot : bouton de liaison, ping, etc."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        # Vue persistante : doit être ré-enregistrée à chaque démarrage pour
        # que le bouton reste cliquable après un redémarrage du bot.
        self.bot.add_view(LinkAccountView(self.bot))


# /createrequestbutton
    @app_commands.command(
        name="createrequestbutton",
        description="Crée le bouton de liaison de compte dans ce salon.",
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def create_request_button(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.channel.send(view=LinkAccountView(self.bot))
        await interaction.delete_original_response()


# /ping
    @app_commands.command(name="ping", description="Voir le ping du bot")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def ping(self, interaction: discord.Interaction):
        latency = round(self.bot.latency * 1000)
        await interaction.response.send_message(
            f"🏓 Pong ! Ma latence est de **{latency}ms**.", ephemeral=True
        )


# /account
    @app_commands.command(
        name="account",
        description="Affiche les infos du compte vAMSYS lié à un membre.",
    )
    @app_commands.describe(membre="Le membre Discord à consulter")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def account(self, interaction: discord.Interaction, membre: discord.Member = None):
        await interaction.response.defer(ephemeral=False)

        user = membre or interaction.user

        record = await self.bot.supabase.get_by_discord_id(str(user.id))

        if record is None:
            await interaction.followup.send(
                f"Aucun compte lié trouvé pour {user.mention}.", ephemeral=True
            )
            return

        await interaction.followup.send(embed=_format_record(record, user), ephemeral=False)


# /removeaccount
    @app_commands.command(
        name="removeaccount",
        description="Supprime l'entrée de liaison d'un membre.",
    )
    @app_commands.describe(membre="Le membre Discord dont il faut supprimer la liaison")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def remove_account(self, interaction: discord.Interaction, membre: discord.Member):
        await interaction.response.defer(ephemeral=True)

        record = await self.bot.supabase.get_by_discord_id(str(membre.id))
        if record is None:
            await interaction.followup.send(
                f"Aucun compte lié trouvé pour {membre.mention}.", ephemeral=True
            )
            return

        deleted = await self.bot.supabase.delete(str(membre.id))

        if not deleted:
            await interaction.followup.send(
                "❌ Erreur lors de la suppression en base.", ephemeral=True
            )
            return

        vamsys_cog: VamsysCog = self.bot.get_cog("VamsysCog")
        discord_note = ""
        if vamsys_cog is not None:
            _, discord_message = await vamsys_cog.remove_pilot_from_member(interaction.guild, membre)
            if discord_message != "OK":
                discord_note = f"\n⚠️ {discord_message}"
        else:
            discord_note = "\n⚠️ Module vAMSYS non chargé : rôle/pseudo non réinitialisés."

        await interaction.followup.send(
            f"✅ Entrée supprimée pour {membre.mention} (`{record.get('skr_id') or '?'}`). "
            f"Rôle retiré et pseudo réinitialisé.{discord_note}",
            ephemeral=True,
        )
    
# /ticketexecutif
    @app_commands.command(name="ticketexecutif", description="Transformer le ticket actuel en un ticket réservé à l'équipe exécutive")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def ticketexecutif(self, interaction: discord.Interaction):
        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Cette commande doit être utilisée dans un salon texte.", ephemeral=True
            )
            return

        allowed_categories = {str(c) for c in config.TICKET_CATEGORY_IDS}
        if allowed_categories and str(channel.category_id) not in allowed_categories:
            await interaction.response.send_message(
                "Ce salon n'est pas dans une catégorie de tickets valide.", ephemeral=True
            )
            return

        if EXEC_NAME_RE.match(channel.name):
            await interaction.response.send_message(
                "Ce ticket est déjà en mode exécutif.", ephemeral=True
            )
            return

        match = TICKET_NAME_RE.match(channel.name)
        if not match:
            await interaction.response.send_message(
                "Ce salon ne ressemble pas à un ticket reconnu "
                "(attendu : ⚪dmd-ID, 🟢pirep-ID ou 🔴sign-ID).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False)

        original_name = channel.name
        ticket_id = match.group(2)
        new_name = f"🟡exec-{ticket_id}"

        try:
            edit_error = await _safe_edit_channel(
                channel,
                name=new_name,
                topic=f"original:{original_name}",
                reason=f"Ticket passé en exécutif par {interaction.user}",
            )
        except Exception:
            edit_error = "❌ Erreur inattendue lors du renommage du salon."
        if edit_error:
            await interaction.followup.send(edit_error, ephemeral=True)
            return

        perm_errors: list[str] = []

        for role_id in config.TICKET_EXEC_DENY_ROLE_IDS:
            role = interaction.guild.get_role(int(role_id)) if role_id.isdigit() else None
            if role is None:
                perm_errors.append(f"rôle introuvable ({role_id})")
                continue
            try:
                await channel.set_permissions(role, view_channel=False)
            except discord.Forbidden:
                perm_errors.append(f"impossible de retirer la vue à {role.name}")

        for role_id in config.TICKET_EXEC_ALLOW_ROLE_IDS:
            role = interaction.guild.get_role(int(role_id)) if role_id.isdigit() else None
            if role is None:
                perm_errors.append(f"rôle introuvable ({role_id})")
                continue
            try:
                await channel.set_permissions(role, view_channel=True)
            except discord.Forbidden:
                perm_errors.append(f"impossible de donner la vue à {role.name}")

        note = f"\n⚠️ {' ; '.join(perm_errors)}" if perm_errors else ""
        await interaction.followup.send(
            f"Votre ticket a été transféré à l'équipe **exécutive** de SkyRiviera.",
            ephemeral=False,
        )

    @ticketexecutif.error
    async def ticketexecutif_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
            )
        else:
            log.exception("Erreur dans /ticketexecutif : %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Une erreur inattendue est survenue.", ephemeral=True
                )


# /ticketrestore
    @app_commands.command(name="ticketrestore", description="Remettre un ticket exécutif dans son état d'origine")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def ticketrestore(self, interaction: discord.Interaction):
        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Cette commande doit être utilisée dans un salon texte.", ephemeral=True
            )
            return

        if not EXEC_NAME_RE.match(channel.name):
            await interaction.response.send_message(
                "Ce salon n'est pas un ticket exécutif actif.", ephemeral=True
            )
            return

        topic_match = ORIGINAL_TOPIC_RE.match(channel.topic or "")
        if not topic_match:
            await interaction.response.send_message(
                "Impossible de retrouver le nom d'origine (description du salon manquante ou "
                "modifiée). Renomme le salon manuellement.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False)

        original_name = topic_match.group(1)

        try:
            edit_error = await _safe_edit_channel(
                channel,
                name=original_name,
                topic=None,
                reason=f"Ticket restauré par {interaction.user}",
            )
        except Exception:
            edit_error = "❌ Erreur inattendue lors du renommage du salon."
        if edit_error:
            await interaction.followup.send(edit_error, ephemeral=True)
            return

        perm_errors: list[str] = []

        for role_id in config.TICKET_EXEC_DENY_ROLE_IDS:
            role = interaction.guild.get_role(int(role_id)) if role_id.isdigit() else None
            if role is None:
                continue
            try:
                await channel.set_permissions(role, view_channel=True)
            except discord.Forbidden:
                perm_errors.append(f"vue non restaurée pour {role.name}")

        # Rôles exécutifs qui avaient un accès exclusif : on retire
        # simplement l'overwrite qu'on avait ajouté (retour à l'héritage).
        for role_id in config.TICKET_EXEC_ALLOW_ROLE_IDS:
            role = interaction.guild.get_role(int(role_id)) if role_id.isdigit() else None
            if role is None:
                continue
            try:
                await channel.set_permissions(role, overwrite=None)
            except discord.Forbidden:
                perm_errors.append(f"permissions non réinitialisées pour {role.name}")

        note = f"\n⚠️ {' ; '.join(perm_errors)}" if perm_errors else ""
        await interaction.followup.send(
            f"Votre ticket a été récupéré par l'équipe **staff** de SkyRiviera.", ephemeral=False
        )

    @ticketrestore.error
    async def ticketrestore_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
            )
        else:
            log.exception("Erreur dans /ticketrestore : %s", error)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Une erreur inattendue est survenue.", ephemeral=True
                )

# /absence
    @app_commands.command(name="absence", description="Signaler une absence")
    @app_commands.describe(
        date_début="Date du début de l'absence (ex: 20/07/2026)",
        date_fin="Date de fin de l'absence (ex: 25/07/2026)",
        motif="Motif de l'absence",
    )
    async def absence(
        self, interaction: discord.Interaction, date_début: str, date_fin: str, motif: str
    ):
        try:
            debut_dt = datetime.strptime(date_début, "%d/%m/%Y")
            fin_dt = datetime.strptime(date_fin, "%d/%m/%Y")
        except ValueError:
            await interaction.response.send_message(
                "❌ Format de date invalide. Utilise le format JJ/MM/AAAA (ex: 20/07/2026).",
                ephemeral=True,
            )
            return

        if fin_dt < debut_dt:
            await interaction.response.send_message(
                "❌ La date de fin ne peut pas être antérieure à la date de début.",
                ephemeral=True,
            )
            return

        try:
            channel_id = int(config.ABSENCE_CHANNEL_ID)
        except (TypeError, ValueError):
            log.error("config.ABSENCE_CHANNEL_ID est invalide : %r", config.ABSENCE_CHANNEL_ID)
            await interaction.response.send_message(
                "❌ Le salon de réception des absences est mal configuré. "
                "Contactez un administrateur.",
                ephemeral=True,
            )
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None

        if channel is None:
            await interaction.response.send_message(
                "❌ Le salon de réception des absences est introuvable. "
                "Contactez un administrateur.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="🛡️ Déclaration d'une absence staff", color=config.EMBED_COLOR)
        embed.add_field(name="Staff", value=interaction.user.mention, inline=False)
        embed.add_field(name="Début", value=date_début, inline=True)
        embed.add_field(name="Fin", value=date_fin, inline=True)
        embed.add_field(name="Motif", value=motif, inline=False)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        embed.add_field(name="─", value=f"Déclaré le <t:{now_ts}:f>", inline=False)

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Le bot n'a pas la permission d'envoyer de message dans le salon des absences.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.exception("Erreur lors de l'envoi du formulaire d'absence : %s", exc)
            await interaction.response.send_message(
                "❌ Erreur lors de l'envoi du formulaire d'absence.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ Ton absence du **{date_début}** au **{date_fin}** a bien été transmise.",
            ephemeral=True,
        )

    @absence.error
    async def absence_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        log.exception("Erreur dans /absence : %s", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Une erreur inattendue est survenue.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(CommandesCog(bot))