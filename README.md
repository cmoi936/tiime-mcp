# tiime-mcp

Serveur [MCP](https://modelcontextprotocol.io) (Model Context Protocol) en Python pour
l'API **interne** de [Tiime](https://www.tiime.fr) — dépôt de justificatifs comptables,
consultation des transactions bancaires et rattachement des pièces.

Un seul fichier, paramétrable **entièrement par variables d'environnement** :
`tiime_mcp_server.py`. Aucun secret, aucun chemin propre à une machine, aucun
identifiant de compte n'est présent dans le dépôt.

> ⚠️ L'API utilisée (`chronos-api.tiime-apps.com/v1`) est l'API interne et **non
> documentée** de l'application web `apps.tiime.fr`. Elle n'est pas officiellement
> supportée et peut changer sans préavis. Projet non affilié à Tiime.

---

## Ce que fait le serveur

Outils exposés au client MCP :

- `tiime_whoami` — utilisateur connecté et sociétés accessibles
- `tiime_list_companies` — sociétés du compte (id à reporter dans la config)
- `tiime_list_categories` — catégories de documents (repérer « Justificatifs »)
- `tiime_list_transactions` — transactions bancaires, filtrables sur le libellé
- `tiime_list_documents` — documents déjà présents
- `tiime_upload_document` — dépôt d'une pièce à l'unité
- `tiime_deposit_batch` — dépôt par lot (fichiers ou dossiers), **simulation par défaut**
- `tiime_link_document` — rattachement d'une pièce à une transaction
- `tiime_get_matchings` — pièces rattachées à une transaction
- `tiime_set_metadata` — métadonnées comptables d'un document
- `tiime_delete_document` — suppression (exige `confirm=True`)

Le dépôt est **idempotent** : Tiime déduplique par contenu de fichier et renvoie le
document existant plutôt que d'en créer un second. Le lot détecte en plus les doublons
« par contenu comptable » (même montant, date proche), que la déduplication par octets
ne voit pas.

---

## Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt        # mcp (SDK officiel), rien d'autre
```

## Configuration

Tout passe par l'environnement — rien à écrire dans le dépôt :

| Variable | Rôle |
|---|---|
| `TIIME_EMAIL` | compte Tiime *(obligatoire, sauf jeton en cache)* |
| `TIIME_PASSWORD` | mot de passe Tiime *(obligatoire, sauf jeton en cache)* |
| `TIIME_COMPANY_ID` | id de la société (évite l'appel de découverte) |
| `TIIME_CATEGORY_ID` | id de la catégorie de dépôt (« Justificatifs ») |
| `TIIME_STATE_DIR` | dossier du cache de jeton · défaut `~/.local/state/tiime-mcp` |
| `TIIME_TOKEN_CACHE` | chemin explicite du cache de jeton |
| `TIIME_APP_VERSION` | en-tête `tiime-app-version` · défaut `4.24.5` |
| `TIIME_MFA_CODE` | code MFA à 6 chiffres, si connu à l'avance |
| `TIIME_MFA_CODE_COMMAND` | commande shell qui **affiche le code MFA sur stdout** |
| `TIIME_API_BASE` / `TIIME_AUTH_BASE` | bases API / Auth0 |
| `TIIME_CLIENT_ID` | `client_id` OAuth public de l'app web |
| `TIIME_TIMEOUT` | délai HTTP en secondes · défaut `60` |
| `TIIME_DEBUG=1` | trace de diagnostic sur stderr |
| `TIIME_TRANSPORT` | `stdio` (défaut), `http` ou `sse` |
| `TIIME_HOST` / `TIIME_PORT` | écoute en transport http · défaut `127.0.0.1:8000` |

**MFA.** Si le compte a le second facteur actif, le flux web Auth0 complet est déroulé
automatiquement. Le code à 6 chiffres est obtenu, dans l'ordre : `TIIME_MFA_CODE`,
puis `TIIME_MFA_CODE_COMMAND` (brancher ici son lecteur d'IMAP, l'API Gmail, un
gestionnaire de mots de passe…), sinon saisie interactive sur stdin. Le `refresh_token`
est ensuite mis en cache (fichier `0600`) et **le MFA n'est plus redemandé**.

---

## Utilisation

Vérifier les identifiants sans démarrer le serveur :

```bash
TIIME_EMAIL=... TIIME_PASSWORD=... python tiime_mcp_server.py --check
```

Démarrer le serveur (stdio) :

```bash
TIIME_EMAIL=... TIIME_PASSWORD=... python tiime_mcp_server.py
```

### Déclaration côté client MCP

```json
{
  "mcpServers": {
    "tiime": {
      "command": "/chemin/vers/.venv/bin/python",
      "args": ["/chemin/vers/tiime_mcp_server.py"],
      "env": {
        "TIIME_EMAIL": "compte@exemple.fr",
        "TIIME_PASSWORD": "...",
        "TIIME_COMPANY_ID": "123456",
        "TIIME_CATEGORY_ID": "654321",
        "TIIME_MFA_CODE_COMMAND": "ma-commande-qui-sort-le-code"
      }
    }
  }
}
```

### Déploiement en HTTP (optionnel)

```bash
TIIME_TRANSPORT=http TIIME_HOST=127.0.0.1 TIIME_PORT=8000 python tiime_mcp_server.py
# endpoint : http://127.0.0.1:8000/mcp
```

---

## Dépôt par lot piloté par un lettrage

`tiime_deposit_batch` accepte un rapport JSON de lettrage qui apporte, par pièce, la date
et le montant :

```json
{"matches": [{"justif": "facture.pdf", "date": "2026-09-03",
              "justif_amount": 120.00, "label": "Fournisseur X"}]}
```

- `lettrage_only=True` restreint le lot aux pièces d'achat citées par le rapport — le
  dossier de travail contient souvent aussi le relevé bancaire, qui n'a rien à faire chez
  Tiime.
- `link=True` rattache chaque pièce à sa transaction bancaire, **uniquement si le candidat
  est unique** (montant égal, date à ±`window` jours, transaction non déjà rattachée).
  En cas d'ambiguïté, rien n'est rattaché : le rattachement manuel reste possible.
- `dry_run=True` par défaut : rien n'est écrit côté Tiime.

Ne **pas** déposer les spécimens / factures fictives : ils créeraient de faux
justificatifs comptables.

---

## Codes retour (`--check`)

`0` ok · `2` usage/config · `3` authentification · `4` erreur API · `5` réseau.

## Sécurité

- Aucun secret dans le dépôt : identifiants et jetons vivent dans l'environnement et
  dans le cache local (`0600`), tous deux listés dans `.gitignore`.
- Le `client_id` Auth0 présent dans le code est **public** : c'est celui de
  l'application web `apps.tiime.fr`, visible dans n'importe quel navigateur.
- Le `refresh_token` donne accès au compte : traiter le cache comme un secret.

## Licence

MIT — voir [LICENSE](LICENSE).
