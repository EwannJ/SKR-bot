"""
Fonction serverless Vercel : callback OAuth PKCE vAMSYS.

Remplace l'ancien couple (serveur aiohttp local dans cogs/vamsys.py + tunnel
ngrok). Comme cette fonction tourne dans un process complètement séparé du
bot Discord, et sans état conservé entre deux appels, elle :

  1. récupère code_verifier / discord_user_id / guild_id depuis le paramètre
     `state`, qui est un jeton signé (HMAC-SHA256) généré par
     VamsysCog.create_pending_login() côté bot (voir utils.create_state_token) ;
  2. échange le code contre un token vAMSYS ;
  3. récupère le profil pilote (rang) et l'identité (nom) ;
  4. écrit le lien en base via l'API REST Supabase ;
  5. applique le pseudo/rôle Discord via l'API REST Discord — pas besoin
     d'une connexion Gateway pour ça, un simple appel HTTP avec le token du
     bot suffit.

Variables d'environnement requises sur Vercel (Project Settings > Environment
Variables) :
    DISCORD_TOKEN        même token que le bot
    SUPABASE_URL         même valeur que côté bot
    SUPABASE_SERVICE_KEY même valeur que côté bot
    VAMSYS_STATE_SECRET  ⚠️ DOIT être identique à config.VAMSYS_STATE_SECRET
                         côté bot, sinon tous les `state` seront rejetés.
    VERCEL_CALLBACK_URL  l'URL publique de cette fonction elle-même, ex :
                         https://ton-projet.vercel.app/api/callback
                         (doit aussi être l'URL enregistrée dans les
                         paramètres OAuth de l'app vAMSYS)

⚠️ SERVERS et LOGIN_TIMEOUT_SECONDS ci-dessous ont une copie jumelle dans
config.py côté bot. Garde les deux synchronisées si tu changes un rôle, un
séparateur de pseudo ou la durée de validité des liens.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

# ---------------------------------------------------------------------------
# Config non sensible (copie jumelle de config.py côté bot)
# ---------------------------------------------------------------------------
VAMSYS_CLIENT_ID = "973"
VAMSYS_TOKEN_URL = "https://vamsys.io/oauth/token"
VAMSYS_PILOT_ME_URL = "https://vamsys.io/api/v3/pilot/profile"
VAMSYS_USER_URL = "https://vamsys.io/api/v3/pilot/user"

SUPABASE_TABLE = "skr_accounts"

LOGIN_TIMEOUT_SECONDS = 600  # doit correspondre à config.LOGIN_TIMEOUT_SECONDS

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

DISCORD_API = "https://discord.com/api/v10"

# ---------------------------------------------------------------------------
# Secrets (variables d'environnement Vercel)
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
STATE_SECRET = os.environ["VAMSYS_STATE_SECRET"]
REDIRECT_URI = os.environ["VERCEL_CALLBACK_URL"]


# ---------------------------------------------------------------------------
# Jeton d'état signé — copie jumelle de utils.create_state_token /
# verify_state_token côté bot. Ne garder que la vérification ici, la
# création se fait côté bot (create_pending_login).
# ---------------------------------------------------------------------------
def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def verify_state_token(token: str, secret: str, max_age_seconds: int) -> dict | None:
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


# ---------------------------------------------------------------------------
# Formatage du nom — copie jumelle de utils.sanitise_name côté bot
# ---------------------------------------------------------------------------
def sanitise_name(raw_name: str) -> str:
    def format_part(part: str) -> str:
        if "-" in part:
            return "-".join(
                section[:1].upper() + section[1:].lower()
                for section in part.split("-")
            )
        return part[:1].upper() + part[1:].lower()

    return " ".join(format_part(part) for part in raw_name.split(" ") if part)


def _compute_team(pilot_data: dict) -> str | None:
    rank = pilot_data.get("rank") or {}
    return rank.get("name")


# ---------------------------------------------------------------------------
# Supabase — appel direct à l'API REST (pas besoin du SDK complet pour un
# simple upsert depuis une fonction sans état)
# ---------------------------------------------------------------------------
def supabase_upsert_link(record: dict) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=discord_user_id"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    try:
        resp = requests.post(url, headers=headers, json=record, timeout=10)
    except requests.RequestException as exc:
        print(f"Erreur réseau upsert Supabase : {exc}")
        return False
    if resp.status_code >= 300:
        print(f"Échec upsert Supabase ({resp.status_code}) : {resp.text}")
        return False
    return True


# ---------------------------------------------------------------------------
# Discord — API REST directe (pas de connexion Gateway nécessaire pour
# éditer le pseudo/les rôles d'un membre, un appel HTTP suffit)
# ---------------------------------------------------------------------------
def discord_get_member(guild_id: str, user_id: str) -> dict | None:
    url = f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=10)
    except requests.RequestException as exc:
        print(f"Erreur réseau récupération membre Discord : {exc}")
        return None
    if resp.status_code != 200:
        print(f"Échec récupération membre Discord ({resp.status_code}) : {resp.text}")
        return None
    return resp.json()


def apply_pilot_to_member(guild_id: str, user_id: str, pilot_data: dict) -> tuple[bool, str]:
    server_config = SERVERS.get(str(guild_id))
    if server_config is None:
        return False, "Serveur non configuré."

    member = discord_get_member(guild_id, user_id)
    if member is None:
        return False, "Membre introuvable sur ce serveur Discord."

    first_name = (pilot_data.get("first_name") or "").strip()
    last_name = (pilot_data.get("last_name") or "").strip()
    pilot_id = pilot_data.get("username") or ""

    last_initial = f"{last_name[0].upper()}." if last_name else ""
    formatted_name = f"{first_name} {last_initial}".strip()
    full_name = sanitise_name(formatted_name)

    separator = server_config["nickSeparator"]
    new_nick = f"{full_name}{separator}{pilot_id}".strip() if full_name else pilot_id

    current_role_ids = [str(rid) for rid in member.get("roles", [])]

    role_removal_cfg = server_config.get("roleRemoval", {"enabled": False, "roleId": []})
    if role_removal_cfg.get("enabled", False):
        to_remove = {str(r) for r in role_removal_cfg.get("roleId", [])}
        current_role_ids = [rid for rid in current_role_ids if rid not in to_remove]

    for role_id in server_config.get("accessRoleId", []):
        if str(role_id) not in current_role_ids:
            current_role_ids.append(str(role_id))

    patch_url = f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = requests.patch(
            patch_url,
            headers=headers,
            json={"nick": new_nick, "roles": current_role_ids},
            timeout=10,
        )
    except requests.RequestException as exc:
        return True, f"Partiel : pseudo/rôles non appliqués (erreur réseau : {exc})"

    if resp.status_code >= 300:
        print(f"Échec édition membre Discord ({resp.status_code}) : {resp.text}")
        return True, f"Partiel : pseudo/rôles non appliqués (code {resp.status_code})"

    return True, "OK"


# ---------------------------------------------------------------------------
# Handler HTTP — format attendu par le runtime Python de Vercel : une classe
# nommée `handler` héritant de BaseHTTPRequestHandler dans /api.
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):
    def _respond(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        code = (query.get("code") or [None])[0]
        state = (query.get("state") or [None])[0]
        error = (query.get("error") or [None])[0]

        if error:
            self._respond(400, f"Autorisation refusée ou erreur vAMSYS : {error}. Tu peux fermer cette page.")
            return

        if not code or not state:
            self._respond(400, "Lien invalide ou expiré. Retourne sur Discord et reclique sur le bouton.")
            return

        payload = verify_state_token(state, STATE_SECRET, LOGIN_TIMEOUT_SECONDS)
        if payload is None:
            self._respond(400, "Lien invalide ou expiré. Retourne sur Discord et reclique sur le bouton.")
            return

        code_verifier = payload["code_verifier"]
        discord_user_id = str(payload["discord_user_id"])
        guild_id = str(payload["guild_id"])

        # --- Échange du code contre un token (pas de client_secret : client PKCE public) ---
        token_payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": VAMSYS_CLIENT_ID,
            "code_verifier": code_verifier,
        }
        try:
            resp = requests.post(VAMSYS_TOKEN_URL, data=token_payload, timeout=10)
        except requests.RequestException as exc:
            self._respond(502, f"Erreur réseau, réessaie plus tard.\n\n[DEBUG] {exc}")
            return

        if resp.status_code != 200:
            print(f"Échec de l'échange de token vAMSYS ({resp.status_code}) : {resp.text}")
            self._respond(502, "Erreur lors de la connexion à vAMSYS. Réessaie.")
            return

        access_token = resp.json().get("access_token")
        if not access_token:
            self._respond(502, "Réponse vAMSYS invalide (pas de token).")
            return

        headers = {"Authorization": f"Bearer {access_token}"}

        # --- Récupération du profil pilote (rang, username) ---
        try:
            profile_resp = requests.get(VAMSYS_PILOT_ME_URL, headers=headers, timeout=10)
        except requests.RequestException as exc:
            self._respond(502, f"Erreur réseau, réessaie plus tard.\n\n[DEBUG] {exc}")
            return

        if profile_resp.status_code != 200:
            self._respond(
                502,
                "Impossible de récupérer ton profil vAMSYS.\n\n"
                f"[DEBUG] Code HTTP : {profile_resp.status_code}\n"
                f"[DEBUG] Réponse vAMSYS : {profile_resp.text}",
            )
            return

        raw_data = profile_resp.json()
        pilot_data = raw_data.get("data", raw_data) if isinstance(raw_data, dict) else raw_data

        # --- Récupération de l'identité (nom) — endpoint séparé, non bloquant ---
        identity_data: dict = {}
        try:
            identity_resp = requests.get(VAMSYS_USER_URL, headers=headers, timeout=10)
            if identity_resp.status_code == 200:
                raw_identity = identity_resp.json()
                identity_data = (
                    raw_identity.get("data", raw_identity)
                    if isinstance(raw_identity, dict)
                    else raw_identity
                )
            else:
                print(f"Échec récupération identité pilote ({identity_resp.status_code}) : {identity_resp.text}")
        except requests.RequestException as exc:
            print(f"Erreur réseau récupération identité pilote : {exc}")

        # --- Fusion des deux réponses : rang depuis /profile, nom depuis /user ---
        first_name = identity_data.get("first_name") or pilot_data.get("first_name") or ""
        last_name = identity_data.get("last_name") or pilot_data.get("last_name") or ""
        skr_id = (
            pilot_data.get("username")
            or (identity_data.get("pilot") or {}).get("username")
            or ""
        )

        # --- Enregistrement en base D'ABORD : si ça échoue, on n'applique
        # ni le pseudo ni le rôle (source de vérité = la base). ---
        db_ok = supabase_upsert_link(
            {
                "discord_user_id": discord_user_id,
                "skr_id": skr_id,
                "first_name": first_name,
                "last_name": last_name.upper(),
                "team": _compute_team(pilot_data),
                "linked_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if not db_ok:
            self._respond(
                500,
                "❌ Erreur lors de l'enregistrement en base de données. "
                "Ton pseudo et ton rôle n'ont pas été modifiés. Réessaie, ou contacte un administrateur.",
            )
            return

        # --- Application du pseudo/rôle côté Discord (seulement si la DB a réussi) ---
        merged_data = {**pilot_data, "first_name": first_name, "last_name": last_name, "username": skr_id}
        success, message = apply_pilot_to_member(guild_id, discord_user_id, merged_data)

        if success and message == "OK":
            self._respond(
                200,
                "✅ Compte lié avec succès ! Ton pseudo et ton rôle ont été mis à jour. "
                "Tu peux fermer cette page et retourner sur Discord.",
            )
        elif success:
            print(f"Liaison partielle pour {discord_user_id} : {message}")
            self._respond(
                200,
                f"✅ Compte lié, mais avec un avertissement : {message}\n\n"
                "Screen cette page, note la date et l'heure et ouvre un ticket.",
            )
        else:
            print(f"Échec de l'application du pseudo/rôle pour {discord_user_id} : {message}")
            self._respond(500, f"Connexion réussie, mais erreur côté Discord : {message}")