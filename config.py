import os

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Variables d'environnement
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_TOKEN")
NGROK_AUTHTOKEN = os.environ.get("NGROK_AUTHTOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "skr_accounts")

for _name, _value in [
    ("DISCORD_TOKEN", TOKEN),
    ("NGROK_AUTHTOKEN", NGROK_AUTHTOKEN),
    ("SUPABASE_URL", SUPABASE_URL),
    ("SUPABASE_SERVICE_KEY", SUPABASE_SERVICE_KEY),
]:
    if not _value:
        raise RuntimeError(f"Variable d'environnement manquante : {_name}")

# ---------------------------------------------------------------------------
# CONFIG NON SENSIBLE
# ---------------------------------------------------------------------------

# Client ID du client OAuth "Authorization Code + PKCE" (pas le "968", qui
# est un client Client Credentials différent)
VAMSYS_CLIENT_ID = "973"

# Domaine statique ngrok (gratuit, fixe tant que tu ne le supprimes pas)
NGROK_DOMAIN = "barbecue-avert-reckless.ngrok-free.dev"

# Port local sur lequel le mini-serveur web tourne (ngrok fait le pont vers
# l'extérieur, donc ce port n'a pas besoin d'être ouvert publiquement sur Orion)
LOCAL_PORT = 8080

REDIRECT_URI = f"https://{NGROK_DOMAIN}/vamsys/callback"

VAMSYS_AUTHORIZE_URL = "https://vamsys.io/oauth/authorize"
VAMSYS_TOKEN_URL = "https://vamsys.io/oauth/token"
VAMSYS_PILOT_ME_URL = "https://vamsys.io/api/v3/pilot/profile"
# Endpoint identité (first_name, last_name, email, discord, réseaux...) — scope identity:basic
VAMSYS_USER_URL = "https://vamsys.io/api/v3/pilot/user"

VAMSYS_SCOPES = "identity:basic identity:discord pilot:read"

# Configuration par serveur Discord : pseudo + rôles à appliquer
SERVERS = {
    "1416847953783558327": {
        "nickSeparator": " | ",
        "accessRoleId": ["1525912891121991822"],
        "roleRemoval": {
            "enabled": False,
            "roleId": [],
        },
    },
}

# Durée de vie max d'une tentative de liaison en attente (secondes)
LOGIN_TIMEOUT_SECONDS = 600  # 10 minutes

# ---------------------------------------------------------------------------
# TICKETS EXÉCUTIFS (/ticketexecutif, /ticketrestore)
# ---------------------------------------------------------------------------
# Catégories dans lesquelles la commande /ticketexecutif est utilisable.
# Laisse la liste vide pour autoriser n'importe quelle catégorie.
TICKET_CATEGORY_IDS: list[str] = [
    "1525919874143227995",
]

# Rôles à qui on retire la vue du salon quand il passe en exécutif
# (ex: rôle Staff général + un autre rôle).
TICKET_EXEC_DENY_ROLE_IDS: list[str] = [
    "1425110909658992864", # Staff
    "1525926738046226513" # PIREP manager
]

# Rôles à qui on donne l'accès exclusif au salon en mode exécutif.
TICKET_EXEC_ALLOW_ROLE_IDS: list[str] = [
    "1525929854208577556", # Directeur des opérations
    "1525905898923626527" # Responsable développement
    "1416864103602978947" # COO
    "1416863443478122646" # CEE
]

# ---------------------------------------------------------------------------
# ABSENCES (/absence)
# ---------------------------------------------------------------------------
# Salon dans lequel le formulaire d'absence est posté.
ABSENCE_CHANNEL_ID = "1525924102336942191"

# EMBED
EMBED_COLOR = 0x0e8694