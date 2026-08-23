import asyncio
import logging

import discord
from discord.ext import commands
from pyngrok import conf, ngrok

import config
from supabase_client import SupabaseClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("skr_bot")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

# Client Supabase partagé, accessible depuis n'importe quel cog via self.bot.supabase
bot.supabase = SupabaseClient(
    url=config.SUPABASE_URL,
    service_key=config.SUPABASE_SERVICE_KEY,
    table=config.SUPABASE_TABLE,
)
log.info("Client Supabase configuré (table: %s)", config.SUPABASE_TABLE)

# Cogs à charger, dans l'ordre : vamsys en premier car commandes en dépend
# (récupération du cog via bot.get_cog("VamsysCog") pour créer les liaisons
# en attente et enregistrer la vue persistante du bouton).
INITIAL_EXTENSIONS = (
    "cogs.vamsys",
    "cogs.commandes",
)


def start_ngrok_tunnel():
    conf.get_default().auth_token = config.NGROK_AUTHTOKEN
    tunnel = ngrok.connect(addr=config.LOCAL_PORT, domain=config.NGROK_DOMAIN)
    log.info("Tunnel ngrok ouvert : %s", tunnel.public_url)


@bot.event
async def on_ready():
    print("SKR online !")

    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commandes syncronisées")
    except Exception as e:
        print(f"⚠️ Erreur lors de la synchronisation des commandes : {e}")


async def main():
    async with bot:
        try:
            for extension in INITIAL_EXTENSIONS:
                await bot.load_extension(extension)

            start_ngrok_tunnel()
            await bot.start(config.TOKEN)
        finally:
            await bot.supabase.close()


if __name__ == "__main__":
    asyncio.run(main())