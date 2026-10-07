#!/usr/bin/env python3
"""tiime-mcp — serveur MCP (Model Context Protocol) pour Tiime.

Expose l'API interne de Tiime (chronos-api.tiime-apps.com/v1) sous forme
d'outils MCP : consultation des societes / categories / transactions /
documents, depot de justificatifs a l'unite ou par lot, rattachement d'une
piece a une transaction bancaire, metadonnees, suppression.

Aucune dependance hors `mcp` (SDK officiel) et la bibliotheque standard.
Toute la configuration passe par des variables d'environnement : le script
ne contient aucun secret, aucun chemin propre a une machine, aucun
identifiant de compte.

------------------------------------------------------------------ variables
TIIME_EMAIL                 compte Tiime (obligatoire sauf jeton en cache)
TIIME_PASSWORD              mot de passe Tiime (obligatoire sauf jeton en cache)
TIIME_COMPANY_ID            id de la societe (evite l'appel de decouverte)
TIIME_CATEGORY_ID           id de la categorie de depot (« Justificatifs »)
TIIME_STATE_DIR             dossier du cache de jeton (defaut ~/.local/state/tiime-mcp)
TIIME_TOKEN_CACHE           chemin explicite du cache de jeton
TIIME_APP_VERSION           en-tete tiime-app-version (defaut 4.24.5)
TIIME_MFA_CODE              code MFA a 6 chiffres, si connu a l'avance
TIIME_MFA_CODE_COMMAND      commande shell qui affiche le code MFA sur stdout
TIIME_API_BASE              base API (defaut https://chronos-api.tiime-apps.com/v1)
TIIME_AUTH_BASE             base Auth0 (defaut https://auth0.tiime.fr)
TIIME_CLIENT_ID             client_id OAuth public de l'app web
TIIME_TIMEOUT               delai HTTP en secondes (defaut 60)
TIIME_DEBUG=1               trace de diagnostic sur stderr
TIIME_TRANSPORT             stdio (defaut) ou http
TIIME_HOST / TIIME_PORT     ecoute en transport http (defaut 127.0.0.1:8000)

------------------------------------------------------------------ execution
  python tiime_mcp_server.py            # serveur MCP sur stdio
  python tiime_mcp_server.py --check    # verifie les identifiants et sort

------------------------------------------------------------------ retour
0 ok | 2 usage/config | 3 authentification | 4 erreur API | 5 reseau

L'API Tiime utilisee ici est interne et non documentee : elle peut changer
sans preavis. Ce projet n'est pas affilie a Tiime.
"""

from __future__ import annotations

import argparse
import datetime
import functools
import json
import mimetypes
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Le SDK MCP a renomme FastMCP en MCPServer dans sa version 2 : on accepte les deux.
try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _MCPServer  # type: ignore
    _MCP_V2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer  # type: ignore
    _MCP_V2 = False

# --------------------------------------------------------------------------- #
# configuration (uniquement par l'environnement)
# --------------------------------------------------------------------------- #

AUTH_BASE = os.environ.get("TIIME_AUTH_BASE", "https://auth0.tiime.fr").rstrip("/")
API_BASE = os.environ.get("TIIME_API_BASE", "https://chronos-api.tiime-apps.com/v1").rstrip("/")

# client_id public de l'application web apps.tiime.fr (visible dans le navigateur).
CLIENT_ID = os.environ.get("TIIME_CLIENT_ID", "iEbsbe3o66gcTBfGRa012kj1Rb6vjAND")
AUDIENCE = os.environ.get("TIIME_AUDIENCE", "https://chronos/")
REALM = os.environ.get("TIIME_REALM", "Chronos-prod-db")
SCOPE = os.environ.get("TIIME_SCOPE", "openid email offline_access")
REDIRECT_URI = os.environ.get("TIIME_REDIRECT_URI", "https://apps.tiime.fr/auth-callback")
APP_VERSION = os.environ.get("TIIME_APP_VERSION", "4.24.5")
TIMEOUT = int(os.environ.get("TIIME_TIMEOUT", "60"))

# En-tetes applicatifs exiges par l'API : a maintenir au fil des versions de Tiime.
APP_HEADERS = {
    "tiime-app": "tiime",
    "tiime-app-platform": "web",
    "tiime-app-version": APP_VERSION,
}

UA = os.environ.get(
    "TIIME_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
)

STATE_DIR = Path(os.environ.get("TIIME_STATE_DIR") or (Path.home() / ".local" / "state" / "tiime-mcp"))
TOKEN_CACHE = Path(os.environ.get("TIIME_TOKEN_CACHE") or (STATE_DIR / "token.json"))

CTX = ssl.create_default_context()
DEBUG = bool(os.environ.get("TIIME_DEBUG"))


class TiimeError(Exception):
    """Erreur portant un code de sortie (0 ok | 2 config | 3 auth | 4 API | 5 reseau)."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def dbg(msg: str) -> None:
    if DEBUG:
        print(f"    [dbg] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# socle HTTP
# --------------------------------------------------------------------------- #

def http(method, url, body=None, headers=None, timeout=None, raw=None):
    """Retourne (status, headers, bytes). Ne leve pas sur les codes HTTP."""
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    data = raw
    if data is None and body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=timeout or TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except urllib.error.URLError as e:
        raise TiimeError(5, f"reseau injoignable: {e.reason}") from e


def jload(b):
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return None


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        raise TiimeError(2, f"{path} illisible: {e}") from e


def save_json(path, obj):
    """Ecrit un JSON en 0600 (le cache contient un refresh_token)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# authentification
# --------------------------------------------------------------------------- #

def _store_token(tok, keep_refresh=None):
    """tok = reponse /oauth/token. keep_refresh : refresh_token precedent conserve
    si Auth0 n'en renvoie pas (sinon le MFA est redemande au renouvellement suivant)."""
    if not tok or "access_token" not in tok:
        return None
    cache = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or keep_refresh,
        "expires_at": int(time.time()) + int(tok.get("expires_in", 0)) - 60,
    }
    save_json(TOKEN_CACHE, cache)
    return cache


def _refresh(cache):
    if not cache.get("refresh_token"):
        return None
    st, _, b = http("POST", f"{AUTH_BASE}/oauth/token", body={
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": cache["refresh_token"],
    })
    body = jload(b)
    if st != 200:
        dbg(f"refresh refuse ({st}: {(body or {}).get('error_description', b[:120])})")
        return None
    return _store_token(body, keep_refresh=cache.get("refresh_token"))


def _password_grant(email, password):
    """Grant password direct sur /oauth/token (modele steampipe-plugin-tiime)."""
    st, _, b = http("POST", f"{AUTH_BASE}/oauth/token", body={
        "grant_type": "password",
        "username": email,
        "password": password,
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "audience": AUDIENCE,
        "realm": REALM,
    })
    body = jload(b) or {}
    if st != 200:
        desc = body.get("error_description") or body.get("error") or b[:200].decode("utf-8", "replace")
        return None, f"grant password refuse ({st}: {desc})"
    return _store_token(body), None


class _AuthSession:
    """Session HTTP avec cookies et redirections manuelles.

    Les pages Auth0 du flux MFA (Universal Login) refusent de fonctionner sans
    les cookies poses par /co/authenticate.
    """

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    def __init__(self):
        import http.cookiejar
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), self._NoRedirect)

    def _call(self, url, data=None, headers=None):
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
        try:
            with self.opener.open(req, timeout=max(TIMEOUT, 90)) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read()
        except urllib.error.URLError as e:
            raise TiimeError(5, f"reseau injoignable: {e.reason}") from e

    def get(self, url, accept="text/html,application/json"):
        return self._call(url, headers={"Accept": accept})

    def post_form(self, url, fields, referer):
        # Accept: text/html obligatoire : Auth0 repond 406 Not Acceptable sinon.
        return self._call(url, urllib.parse.urlencode(fields).encode(),
                          {"Content-Type": "application/x-www-form-urlencoded",
                           "Accept": "text/html,application/xhtml+xml",
                           "Origin": AUTH_BASE, "Referer": referer})

    def post_json(self, url, obj, origin="https://apps.tiime.fr"):
        return self._call(url, json.dumps(obj).encode(),
                          {"Content-Type": "application/json",
                           "Origin": origin, "Referer": origin + "/"})


def _html_state(body):
    m = re.search(r'name="state" value="([^"]+)"', body.decode("utf-8", "replace"))
    if not m:
        raise TiimeError(3, "page Auth0 inattendue : 'state' introuvable")
    return m.group(1)


def _html_page_text(body):
    s = body.decode("utf-8", "replace")
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def _obtenir_code_mfa(sent_at):
    """Code MFA a 6 chiffres, par ordre de priorite :

    1. TIIME_MFA_CODE            (utile en script, valeur ephemeres)
    2. TIIME_MFA_CODE_COMMAND    (commande shell : lecteur d'IMAP, API Gmail,
                                  gestionnaire de mots de passe, ... — le code
                                  doit sortir sur stdout)
    3. saisie interactive sur stdin (l'invite est ecrite sur stderr)
    """
    code = (os.environ.get("TIIME_MFA_CODE") or "").strip()
    if code:
        return code

    cmd = os.environ.get("TIIME_MFA_CODE_COMMAND")
    if cmd:
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
        except Exception as e:
            raise TiimeError(3, f"TIIME_MFA_CODE_COMMAND en echec: {e}") from e
        codes = re.findall(r"\b(\d{6})\b", out.stdout or "")
        if codes:
            return codes[0]
        raise TiimeError(3, f"TIIME_MFA_CODE_COMMAND n'a pas retourne de code a 6 chiffres "
                            f"(stderr: {(out.stderr or '')[:160]})")

    print("Code de verification Tiime (6 chiffres) : ", end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    codes = re.findall(r"\b(\d{6})\b", line or "")
    if not codes:
        raise TiimeError(3, "aucun code MFA fourni")
    return codes[0]


def _mfa_login(email, password):
    """Flux Auth0 complet avec second facteur : co/authenticate -> /authorize ->
    choix du facteur (e-mail) -> code -> /oauth/token."""
    import secrets as _s
    s = _AuthSession()

    st, _, b = s.post_json(f"{AUTH_BASE}/co/authenticate", {
        "client_id": CLIENT_ID, "username": email, "password": password,
        "realm": REALM,
        "credential_type": "http://auth0.com/oauth/grant-type/password-realm"})
    ticket = (jload(b) or {}).get("login_ticket")
    if not ticket:
        body = jload(b) or {}
        desc = body.get("description") or body.get("error_description") or b[:200].decode("utf-8", "replace")
        return None, f"co/authenticate ({st}) : {desc}"

    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": SCOPE, "audience": AUDIENCE, "realm": REALM,
        "state": _s.token_hex(8), "nonce": _s.token_hex(8),
        "login_ticket": ticket}, quote_via=urllib.parse.quote)
    st, h, _ = s.get(f"{AUTH_BASE}/authorize?{q}")
    dbg(f"/authorize -> HTTP {st} | Location: {str(h.get('Location'))[:130]}")
    if not h.get("Location"):
        return None, f"/authorize sans redirection (HTTP {st})"
    page = urllib.parse.urljoin(AUTH_BASE, h["Location"])
    st, _, b = s.get(page)
    state = _html_state(b)

    # Basculer sur les autres facteurs si Auth0 propose d'abord le SMS.
    if "mfa-login-options" not in page:
        st, h, _ = s.post_form(page, {"state": state, "action": "pick-authenticator"}, page)
        dbg(f"pick-authenticator -> HTTP {st} | Location: {str(h.get('Location'))[:130]}")
        if not h.get("Location"):
            return None, f"impossible de lister les facteurs MFA (HTTP {st})"
        page = urllib.parse.urljoin(AUTH_BASE, h["Location"])
        st, _, b = s.get(page)
        state = _html_state(b)

    if "mfa-login-options" not in page:
        return None, f"page de choix de facteur inattendue ({page.split('?')[0]})"

    html = b.decode("utf-8", "replace")
    m = re.search(r'value="(email::\d+)"', html)
    if not m:
        return None, ("aucun facteur e-mail inscrit : seul le SMS est disponible "
                      "(ajouter l'e-mail dans les facteurs MFA du compte Tiime)")
    action = m.group(1)

    sent_at = time.time()
    st, h, _ = s.post_form(page, {"state": state, "action": action}, page)
    dbg(f"envoi code -> HTTP {st} | Location: {str(h.get('Location'))[:130]}")
    if not h.get("Location"):
        return None, f"l'envoi du code par e-mail a echoue (HTTP {st})"
    page = urllib.parse.urljoin(AUTH_BASE, h["Location"])
    st, _, b = s.get(page)
    state = _html_state(b)
    dbg(f"page de saisie : {_html_page_text(b)[:150]}")

    code = _obtenir_code_mfa(sent_at)

    st, h, _ = s.post_form(page, {"state": state, "code": code, "action": "default"}, page)
    dbg(f"validation code -> HTTP {st} | Location: {str(h.get('Location'))[:130]}")
    loc, hops = h.get("Location"), 0
    while loc and hops < 8:
        url = urllib.parse.urljoin(AUTH_BASE, loc)
        if url.startswith(REDIRECT_URI.split("/auth-callback")[0]):
            break
        st, h, b = s.get(url)
        loc = h.get("Location")
        dbg(f"  saut {hops + 1}: HTTP {st} -> {str(loc)[:110]}")
        hops += 1
    if not loc:
        return None, f"code refuse ou redirection interrompue (HTTP {st}) : {_html_page_text(b)[:200]}"

    auth_code = urllib.parse.parse_qs(urllib.parse.urlparse(
        urllib.parse.urljoin(AUTH_BASE, loc)).query).get("code", [None])[0]
    if not auth_code:
        return None, f"pas de code d'autorisation dans {loc[:160]}"

    st, _, b = http("POST", f"{AUTH_BASE}/oauth/token", body={
        "grant_type": "authorization_code", "client_id": CLIENT_ID,
        "code": auth_code, "redirect_uri": REDIRECT_URI})
    body = jload(b) or {}
    if st != 200:
        return None, f"echange du code refuse ({st}: {body.get('error_description', b[:150])})"
    return _store_token(body), None


def get_token(force_login=False):
    cache = None if force_login else load_json(TOKEN_CACHE)
    if cache and cache.get("access_token") and cache.get("expires_at", 0) > time.time():
        return cache["access_token"]

    if cache:
        dbg("jeton expire, tentative de refresh...")
        refreshed = _refresh(cache)
        if refreshed:
            dbg("refresh OK")
            return refreshed["access_token"]

    email = os.environ.get("TIIME_EMAIL")
    password = os.environ.get("TIIME_PASSWORD")
    if not email or not password:
        raise TiimeError(2, "identifiants absents : definir TIIME_EMAIL et TIIME_PASSWORD "
                            "(ou fournir un TIIME_TOKEN_CACHE deja rempli)")

    dbg("login Auth0 (grant password)...")
    tok, err = _password_grant(email, password)
    if tok:
        return tok["access_token"]

    if "multifactor" not in err.lower():
        raise TiimeError(3, f"authentification impossible.\n  grant password : {err}")

    dbg(f"{err} -> flux Auth0 complet (MFA)")
    tok, err2 = _mfa_login(email, password)
    if tok:
        return tok["access_token"]

    raise TiimeError(3, f"authentification impossible.\n  grant password : {err}\n  MFA : {err2}")


def api(method, path, body=None, accept=None, extra=None, timeout=None):
    """Appel API authentifie. `path` commence par '/'."""
    token = get_token()
    headers = dict(APP_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    headers.update(extra or {})
    st, hdrs, b = http(method, f"{API_BASE}{path}", body=body, headers=headers, timeout=timeout)
    if st == 401:
        dbg("401 -> jeton rejete, nouvelle authentification...")
        token = get_token(force_login=True)
        headers["Authorization"] = f"Bearer {token}"
        st, hdrs, b = http(method, f"{API_BASE}{path}", body=body, headers=headers, timeout=timeout)
    payload = jload(b)
    if st >= 400:
        raise TiimeError(4, f"{method} {path} -> HTTP {st}: "
                            f"{(payload or b[:300].decode('utf-8', 'replace'))}")
    return payload, hdrs


def _post_multipart(path, form_bytes, accept, content_type, timeout=None):
    token = get_token()
    headers = dict(APP_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    headers["Accept"] = accept
    headers["Content-Type"] = content_type
    st, _, b = http("POST", f"{API_BASE}{path}", raw=form_bytes, headers=headers,
                    timeout=timeout or max(TIMEOUT, 180))
    if st == 401:
        token = get_token(force_login=True)
        headers["Authorization"] = f"Bearer {token}"
        st, _, b = http("POST", f"{API_BASE}{path}", raw=form_bytes, headers=headers,
                        timeout=timeout or max(TIMEOUT, 180))
    payload = jload(b)
    if st >= 400:
        raise TiimeError(4, f"POST {path} -> HTTP {st}: "
                            f"{(payload or b[:300].decode('utf-8', 'replace'))}")
    return payload


# --------------------------------------------------------------------------- #
# resolution societe / categorie
# --------------------------------------------------------------------------- #

def resolve_company():
    cid = os.environ.get("TIIME_COMPANY_ID")
    if cid:
        return int(cid)
    # /companies est la source fiable ; /users/me n'expose que 'active_company'.
    comps, _ = api("GET", "/companies")
    if isinstance(comps, list) and comps:
        if len(comps) > 1:
            dbg("plusieurs societes, choix de la premiere : "
                + ", ".join(f"{c.get('id')}={c.get('name')}" for c in comps)
                + " (definir TIIME_COMPANY_ID pour lever l'ambiguite)")
        return int(comps[0]["id"])
    me, _ = api("GET", "/users/me")
    if (me or {}).get("active_company"):
        return int(me["active_company"])
    comps = (me or {}).get("companies") or []
    if comps:
        return int(comps[0]["id"])
    raise TiimeError(4, "aucune societe retournee par /companies ni /users/me")


def resolve_category(company_id, wanted=None):
    cid = wanted or os.environ.get("TIIME_CATEGORY_ID")
    if cid:
        return int(cid)
    _, cats = list_categories(company_id)
    just = [c for c in cats
            if "justificatif" in str(c.get("name", "")).lower()
            or "receipt" in str(c.get("name", "")).lower()]
    if just:
        return int(just[0]["id"])
    raise TiimeError(2, "categorie 'Justificatifs' introuvable : passer category_id "
                        "ou definir TIIME_CATEGORY_ID apres avoir appele tiime_list_categories")


def list_categories(company_id=None):
    company_id = company_id or resolve_company()
    cats, _ = api("GET", f"/companies/{company_id}/document_categories")
    return company_id, (cats or [])


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #

def upload_document(company_id, category_id, filepath, filename=None, dry_run=False):
    if not os.path.isfile(filepath):
        raise TiimeError(2, f"fichier introuvable: {filepath}")
    filename = filename or os.path.basename(filepath)
    with open(filepath, "rb") as f:
        content = f.read()
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    if dry_run:
        return {"id": None, "_dry_run": True, "filename": filename, "size": len(content)}

    boundary = "----TiimeMcp" + os.urandom(12).hex()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    return _post_multipart(
        f"/companies/{company_id}/document_categories/{category_id}/documents",
        head + content + tail,
        accept="application/vnd.tiime.documents.v3+json,application/vnd.tiime.docs.imputation+json",
        content_type=f"multipart/form-data; boundary={boundary}")


def link_document(company_id, transaction_id, document_id, dry_run=False):
    if dry_run:
        return {"_dry_run": True, "transaction": transaction_id, "document": document_id}
    payload, _ = api(
        "PUT",
        f"/companies/{company_id}/bank_transactions/{transaction_id}/document_matchings",
        body={"documents": [{"id": int(document_id)}]})
    return payload


def set_metadata(company_id, document_id, date=None, wording=None, amount=None,
                 vat_amount=None, dry_run=False):
    """PATCH .../documents/{id} — rend le justificatif exploitable en compta.
    ATTENTION : ecrase aussi le `name` du document quand `wording` est fourni."""
    meta = []
    if date:
        meta.append({"key": "date", "type": "datetime", "value": date})
    if wording:
        meta.append({"key": "wording", "type": "string", "value": wording})
    if amount is not None:
        meta.append({"key": "amount", "type": "amount",
                     "value": {"currency": "EUR", "value": float(amount)}})
    if vat_amount is not None:
        meta.append({"key": "vat_amount", "type": "amount",
                     "value": {"currency": "EUR", "value": float(vat_amount)}})
    if not meta:
        return None
    if dry_run:
        return {"_dry_run": True, "metadata": meta}
    payload, _ = api("PATCH", f"/companies/{company_id}/documents/{document_id}",
                     body={"metadata": meta})
    return payload


def _deja_present(doc, run_started, force=False):
    """True si le POST a renvoye un document PREEXISTANT.

    Tiime deduplique par contenu de fichier : re-deposer un PDF identique ne cree
    pas de doublon, l'API renvoie le document existant avec son created_at d'origine.
    Dans ce cas il ne faut pas rejouer le PATCH de metadonnees : il ecraserait
    l'intitule du document deja en place.
    """
    created = str((doc or {}).get("created_at") or "")
    return bool(created and created < run_started and not force)


def _tiime_documents(company_id):
    docs, start = [], 0
    while True:
        r, h = api("GET", f"/companies/{company_id}/documents?hide_refused=false",
                   extra={"Range": f"items={start}-{start + 99}"})
        part = r if isinstance(r, list) else (r or {}).get("documents", [])
        docs += part
        if len(part) < 100:
            break
        start += 100
    return docs


def _index_documents(docs):
    """{montant arrondi -> [(id, date_ocr, nom)]} pour la detection de doublons."""
    idx = {}
    for d in docs:
        md = {m.get("key"): m.get("value") for m in (d.get("metadata") or [])}
        amt = md.get("amount")
        amt = amt.get("value") if isinstance(amt, dict) else amt
        try:
            amt = round(float(amt), 2)
        except Exception:
            continue
        date = str(md.get("date") or "")[:10]
        idx.setdefault(amt, []).append((d.get("id"), date, str(d.get("name") or "")[:36]))
    return idx


def _suspect_doublon(idx, amount, iso_date, window):
    """Document deja present avec le MEME montant et une date proche : la
    deduplication de Tiime porte sur les octets, pas sur le contenu comptable."""
    if amount is None:
        return []
    try:
        d0 = datetime.date.fromisoformat(str(iso_date)[:10])
    except Exception:
        return []
    out = []
    for doc_id, date, nom in idx.get(round(float(amount), 2), []):
        try:
            if abs((datetime.date.fromisoformat(date) - d0).days) <= window:
                out.append((doc_id, date, nom))
        except Exception:
            continue
    return out


def _tiime_transactions(company_id, fetch=500):
    rows, start = [], 0
    while start < fetch:
        res, h = api("GET", f"/companies/{company_id}/bank_transactions?hide_refused=false",
                     extra={"Range": f"items={start}-{start + 99}"})
        part = res if isinstance(res, list) else (res or {}).get("transactions", [])
        rows += part
        cr = str(h.get("Content-range") or h.get("Content-Range") or "")
        if len(part) < 100 or cr.endswith("/*"):
            break
        start += 100
    return rows


def _candidat_transaction(txs, amount, iso_date, window, deja_liees):
    """Transaction non deja rattachee correspondant au montant et a la date.
    Ne renvoie une transaction que si le candidat est UNIQUE : en cas d'ambiguite
    on ne rattache rien (une erreur de lettrage coute plus cher qu'un oubli)."""
    try:
        d0 = datetime.date.fromisoformat(str(iso_date)[:10])
    except Exception:
        return None, "date inexploitable"
    tol = 0.01
    cands = []
    for t in txs:
        if t.get("id") in deja_liees:
            continue
        if abs(abs(float(t.get("amount") or 0)) - amount) > tol:
            continue
        try:
            d1 = datetime.date.fromisoformat(str(t.get("transaction_date"))[:10])
        except Exception:
            continue
        if abs((d1 - d0).days) > window:
            continue
        cands.append(t)
    if not cands:
        return None, "aucune transaction correspondante"
    if len(cands) > 1:
        ids = ", ".join(str(c.get("id")) for c in cands)
        return None, f"{len(cands)} candidats ambigus ({ids})"
    return cands[0], None


def _load_lettrage(path):
    """{nom de fichier -> {date, amount, label}} depuis un rapport de lettrage."""
    d = load_json(path)
    if not isinstance(d, dict):
        raise TiimeError(2, f"lettrage illisible : {path}")
    plan = {}
    for m in d.get("matches") or []:
        f = m.get("justif")
        if not f:
            continue
        plan[str(f)] = {
            "date": str(m.get("date") or "")[:10],
            "amount": abs(float(m.get("justif_amount") or 0)),
            "label": str(m.get("label") or "")[:60],
        }
    return plan, d


def deposit(paths, ext="pdf", lettrage=None, lettrage_only=False, link=False,
            window=5, category_id=None, skip_suspects=False, dry_run=False):
    """Depot par lot pilote par un rapport de lettrage (modele de la routine mensuelle)."""
    company_id = resolve_company()
    category_id = resolve_category(company_id, category_id)

    files = []
    for p in paths:
        if os.path.isdir(p):
            exts = tuple("." + e.strip().lstrip(".").lower() for e in str(ext).split(","))
            files += [os.path.join(p, f) for f in sorted(os.listdir(p)) if f.lower().endswith(exts)]
        elif os.path.isfile(p):
            files.append(p)
        else:
            raise TiimeError(2, f"chemin introuvable : {p}")
    if not files:
        raise TiimeError(2, "aucun fichier a deposer")

    plan, rapport = {}, {}
    if lettrage:
        plan, rapport = _load_lettrage(lettrage)

    # Ne deposer que les pieces d'achat : le dossier de travail du lettrage contient
    # aussi le releve bancaire, qui n'a rien a faire chez Tiime.
    if lettrage_only and rapport:
        connus = set(plan)
        for k in ("justifs_non_utilises", "justifs_utilises", "justifs_illisibles"):
            for j in rapport.get(k) or []:
                connus.add(j if isinstance(j, str) else str(j.get("file") or j))
        files = [f for f in files if os.path.basename(f) in connus]

    avertissements = []
    try:
        idx_docs = _index_documents(_tiime_documents(company_id))
    except TiimeError as e:
        # La liste des documents ne sert qu'a la detection de doublons : son echec
        # ne doit pas empecher un depot (ni une simulation sans identifiants).
        idx_docs = {}
        avertissements.append(f"liste des documents indisponible ({e}) : "
                              f"controle anti-doublon desactive")
    txs = []
    if link:
        try:
            txs = _tiime_transactions(company_id)
        except TiimeError as e:
            avertissements.append(f"transactions indisponibles ({e}) : rattachement desactive")
            link = False

    deja_liees, resultats = set(), []
    run_started = time.strftime("%Y-%m-%d %H:%M:%S")

    for f in files:
        nom = os.path.basename(f)
        info = plan.get(nom, {})
        ligne = {"fichier": nom, "statut": "?", "doc_id": None, "tx": None,
                 "montant": info.get("amount"), "note": "", "suspects": []}

        # Controle par le CONTENU comptable (montant + date), pas par les octets.
        suspects = _suspect_doublon(idx_docs, info.get("amount"), info.get("date"), window)
        if suspects:
            ligne["suspects"] = suspects
            if skip_suspects:
                ligne["statut"] = "IGNORE"
                ligne["note"] = "doublon probable"
                resultats.append(ligne)
                continue

        doc = upload_document(company_id, category_id, f, dry_run=dry_run)
        doc_id = doc.get("id") if isinstance(doc, dict) else None
        if doc_id is None and not dry_run:
            ligne["statut"] = "ECHEC"
            ligne["note"] = "reponse sans id"
            resultats.append(ligne)
            continue
        ligne["doc_id"] = doc_id
        nouveau = dry_run or not _deja_present(doc, run_started)
        ligne["statut"] = ("NOUVEAU" if dry_run else
                           ("NOUVEAU" if nouveau else "DEJA PRESENT"))

        # Opt-in et deconseille : la date du lettrage est la date BANCAIRE, pas la date
        # de la piece ; Tiime OCRise lui-meme la vraie date a l'ingestion.
        if link and info and not dry_run:
            tx, err = _candidat_transaction(txs, info["amount"], info["date"], window, deja_liees)
            if tx:
                link_document(company_id, tx["id"], doc_id)
                deja_liees.add(tx["id"])
                ligne["tx"] = tx["id"]
            else:
                ligne["note"] = err
        resultats.append(ligne)

    n_new = sum(1 for r in resultats if r["statut"] == "NOUVEAU")
    n_dup = sum(1 for r in resultats if r["statut"] == "DEJA PRESENT")
    n_err = sum(1 for r in resultats if r["statut"] == "ECHEC")
    n_lie = sum(1 for r in resultats if r["tx"])
    n_susp = sum(1 for r in resultats if r.get("suspects"))
    n_ign = sum(1 for r in resultats if r["statut"] == "IGNORE")
    return {
        "company_id": company_id, "category_id": category_id,
        "dry_run": dry_run, "fichiers": len(files),
        "nouveaux": n_new, "deja_presents": n_dup, "echecs": n_err,
        "rattaches": n_lie, "doublons_probables": n_susp, "ignores": n_ign,
        "avertissements": avertissements,
        "details": resultats,
    }


_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def meta_date(iso):
    """'2026-09-01' -> 'Tue Sep 01 2026' (format attendu par l'API).

    Noms de jours/mois en dur : strftime depend de la locale, non garantie sur
    le serveur.
    """
    y, m, d = (int(x) for x in str(iso)[:10].split("-"))
    return f"{_DAY[datetime.date(y, m, d).weekday()]} {_MON[m - 1]} {d:02d} {y}"


# --------------------------------------------------------------------------- #
# serveur MCP
# --------------------------------------------------------------------------- #

HOST = os.environ.get("TIIME_HOST", "127.0.0.1")
PORT = int(os.environ.get("TIIME_PORT", "8000"))

if _MCP_V2:
    mcp = _MCPServer("tiime", instructions="Outils Tiime (justificatifs, transactions).")
else:  # mcp 1.x : l'ecoute http se configure a la construction
    mcp = _MCPServer("tiime", host=HOST, port=PORT)


def _outil(fn):
    """Rend une erreur Tiime lisible par le client MCP plutot que de la laisser filer
    en exception non geree (un modele doit pouvoir lire le motif et corriger)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TiimeError as e:
            return {"erreur": str(e), "code": e.code}
    return wrapper


@mcp.tool()
@_outil
def tiime_whoami() -> dict:
    """Utilisateur connecte et societes accessibles (id, nom, siret, e-mails de depot)."""
    me, _ = api("GET", "/users/me")
    comps, _ = api("GET", "/companies")
    return {"user": me, "companies": comps, "company_id_utilise": resolve_company()}


@mcp.tool()
@_outil
def tiime_list_companies() -> list:
    """Liste les societes du compte (id a reporter dans TIIME_COMPANY_ID)."""
    comps, _ = api("GET", "/companies")
    return comps or []


@mcp.tool()
@_outil
def tiime_list_categories(company_id: int | None = None) -> list:
    """Categories de documents de la societe (reperer « Justificatifs » et son id)."""
    _, cats = list_categories(company_id)
    return cats


@mcp.tool()
@_outil
def tiime_list_transactions(search: str | None = None, limit: int = 50) -> list:
    """Transactions bancaires, filtrees sur le libelle si `search` est fourni."""
    cid = resolve_company()
    rows, _ = api("GET", f"/companies/{cid}/bank_transactions?hide_refused=false",
                  extra={"Range": f"items=0-{max(int(limit) - 1, 0)}"})
    rows = rows or []
    if search:
        rows = [r for r in rows if str(search).lower() in str(r.get("wording", "")).lower()]
    return rows


@mcp.tool()
@_outil
def tiime_list_documents(limit: int = 100) -> list:
    """Documents deja presents chez Tiime (id, nom, categorie)."""
    cid = resolve_company()
    docs = _tiime_documents(cid)
    return docs[: int(limit)]


@mcp.tool()
@_outil
def tiime_upload_document(file_path: str, category_id: int | None = None,
                          dry_run: bool = False) -> dict:
    """Depose un justificatif (chemin local) dans la categorie visee.

    Le depot est idempotent : Tiime deduplique par contenu de fichier et renvoie le
    document existant le cas echeant."""
    cid = resolve_company()
    cat = resolve_category(cid, category_id)
    return upload_document(cid, cat, file_path, dry_run=dry_run)


@mcp.tool()
@_outil
def tiime_deposit_batch(paths: list[str], ext: str = "pdf", lettrage_path: str | None = None,
                        lettrage_only: bool = False, link: bool = False, window: int = 5,
                        category_id: int | None = None, skip_suspects: bool = False,
                        dry_run: bool = True) -> dict:
    """Depot par lot de justificatifs (fichiers ou dossiers).

    `lettrage_path` : rapport JSON de lettrage ({matches:[{justif,date,justif_amount,label}]})
    qui apporte date et montant par piece. `lettrage_only` restreint le lot aux pieces
    d'achat citees par ce rapport. `link` rattache chaque piece a sa transaction bancaire
    (uniquement si le candidat est sans ambiguite). `dry_run=True` par defaut : aucune
    ecriture cote Tiime."""
    return deposit(paths, ext=ext, lettrage=lettrage_path, lettrage_only=lettrage_only,
                   link=link, window=int(window), category_id=category_id,
                   skip_suspects=skip_suspects, dry_run=dry_run)


@mcp.tool()
@_outil
def tiime_link_document(transaction_id: str, document_id: str,
                        dry_run: bool = False) -> dict:
    """Rattache un document existant a une transaction bancaire."""
    return link_document(resolve_company(), transaction_id, document_id, dry_run=dry_run)


@mcp.tool()
@_outil
def tiime_get_matchings(transaction_id: str) -> dict:
    """Documents deja rattaches a une transaction bancaire."""
    cid = resolve_company()
    res, _ = api("GET", f"/companies/{cid}/bank_transactions/{transaction_id}/matchings")
    return res or {}


@mcp.tool()
@_outil
def tiime_set_metadata(document_id: str, date_iso: str | None = None,
                       wording: str | None = None, amount: float | None = None,
                       vat_amount: float | None = None, dry_run: bool = False) -> dict:
    """Pose les metadonnees comptables d'un document (date ISO AAAA-MM-JJ, libelle, montants).

    ATTENTION : `wording` ecrase aussi le nom du document. Deconseille en routine :
    Tiime OCRise la vraie date de la piece a l'ingestion."""
    return set_metadata(resolve_company(), document_id,
                        date=meta_date(date_iso) if date_iso else None,
                        wording=wording, amount=amount, vat_amount=vat_amount,
                        dry_run=dry_run)


@mcp.tool()
@_outil
def tiime_delete_document(document_id: str, confirm: bool = False,
                          dry_run: bool = False) -> dict:
    """Supprime un document (reversible par API mais destructif : exige confirm=True)."""
    if not confirm and not dry_run:
        raise TiimeError(2, f"suppression de {document_id} non confirmee : passer confirm=True")
    cid = resolve_company()
    if dry_run:
        return {"_dry_run": True, "delete": document_id}
    token = get_token()
    headers = dict(APP_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    st, _, b = http("DELETE", f"{API_BASE}/companies/{cid}/documents/{document_id}",
                    headers=headers)
    if st in (200, 202, 204):
        return {"deleted": document_id, "http_status": st}
    payload = jload(b)
    raise TiimeError(4, f"DELETE /companies/{cid}/documents/{document_id} -> HTTP {st}: "
                        f"{(payload or b[:300].decode('utf-8', 'replace'))}")


# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(
        description="Serveur MCP pour l'API interne Tiime (non officielle).")
    p.add_argument("--check", action="store_true",
                   help="verifie l'authentification et affiche l'utilisateur, puis sort")
    p.add_argument("--transport", default=os.environ.get("TIIME_TRANSPORT", "stdio"),
                   choices=["stdio", "http", "sse"], help="transport MCP (defaut stdio)")
    a = p.parse_args()

    try:
        if a.check:
            tok = get_token(force_login=True)
            me, _ = api("GET", "/users/me")
            comps, _ = api("GET", "/companies")
            print(f"OK — jeton obtenu ({len(tok)} caracteres)")
            print(f"utilisateur : {me.get('firstname')} {me.get('lastname')} <{me.get('email')}>")
            for c in comps or []:
                print(f"  societe : id={c.get('id')} {c.get('name')} siret={c.get('siret')}")
            return 0
    except TiimeError as e:
        print(f"ERREUR: {e}", file=sys.stderr, flush=True)
        return e.code

    transport = {"http": "streamable-http"}.get(a.transport, a.transport)
    if _MCP_V2:
        mcp.run(transport, host=HOST, port=PORT) if transport != "stdio" else mcp.run("stdio")
    else:
        mcp.run(transport=transport)
    return 0


if __name__ == "__main__":
    sys.exit(main())
