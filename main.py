import asyncio
import logging

import discord
from discord.ext import commands, tasks
from pyngrok import conf, ngrok

import config
from utils import SupabaseClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("skr_bot")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

INITIAL_EXTENSIONS = (
    "cogs.vamsys",
    "cogs.commandes",
)


def start_ngrok_tunnel():
    conf.get_default().auth_token = config.NGROK_AUTHTOKEN
    tunnel = ngrok.connect(addr=config.LOCAL_PORT, domain=config.NGROK_DOMAIN)
    log.info("Tunnel ngrok ouvert : %s", tunnel.public_url)


@tasks.loop(hours=24)
async def anti_afk_supabase():
    """Anti-AFK : maintient l'activité DB pour éviter la mise en pause
    automatique du projet Supabase (free tier -> pause après 7 jours sans
    activité). Toutes les 24h laisse une marge de sécurité confortable."""
    ok = await bot.supabase.ping()
    if ok:
        log.info("Ping anti-AFK Supabase OK")
    else:
        log.warning("Ping anti-AFK Supabase échoué")


@anti_afk_supabase.before_loop # type: ignore
async def before_anti_afk_supabase():
    # Évite que la boucle démarre avant que bot.supabase soit initialisé.
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print("SKR online !")

    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commandes syncronisées")
    except Exception as e:
        print(f"⚠️ Erreur lors de la synchronisation des commandes : {e}")

    if not anti_afk_supabase.is_running(): # type: ignore
        anti_afk_supabase.start() # type: ignore


async def main():
    bot.supabase = await SupabaseClient.create(
        url=config.SUPABASE_URL, # type: ignore
        service_key=config.SUPABASE_SERVICE_KEY, # type: ignore
        table=config.SUPABASE_TABLE,
    )
    log.info("Client Supabase configuré (table: %s)", config.SUPABASE_TABLE)

    async with bot:
        try:
            for extension in INITIAL_EXTENSIONS:
                await bot.load_extension(extension)

            start_ngrok_tunnel()
            await bot.start(config.TOKEN)
        finally:
            anti_afk_supabase.cancel() # type: ignore
            await bot.supabase.close()


if __name__ == "__main__":
    asyncio.run(main())