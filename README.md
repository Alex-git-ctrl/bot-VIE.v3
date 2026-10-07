# bot-VIE.v3

Bot qui envoie un email dès qu'une nouvelle offre de VIE apparaît.

## Sources surveillées

| Source | Ce qui est lu | Comment |
|---|---|---|
| **Business France** | Offres Finance / Banque, Amériques + Asie‑Pacifique (filtre `BF_PAYLOAD`) | API Civiweb appelée depuis Chromium (Playwright), car l'API rejette les clients non‑navigateur |
| **Société Générale** | Toutes les offres V.I.E du site carrières | Moteur de recherche interne « Quantum » via le proxy du site (`get-token` + `search-proxy.php`), avec `curl_cffi` pour passer Imperva |
| **BNP Paribas** | Toutes les offres VIE (CIB, BGL, Arval, Cardif…) | Liste VIE rendue côté serveur, lue avec `curl_cffi` (Akamai bloque `requests` et Chromium headless) |
| **Natixis** | Toutes les offres VIE du portail Groupe BPCE : Natixis CIB, Natixis IM, AEW, Ostrum, Mirova… | API REST publique `wp-json/bpce/v1/search/jobs` |

Les offres d'une banque déjà présentes chez Business France (même entreprise,
titre très proche) sont écartées pour éviter les doublons dans le mail.

## Fonctionnement

- Le workflow est programmé toutes les 15 minutes (`7,22,37,52 * * * *`), mais
  GitHub ne respecte pas ce rythme : en pratique il passe toutes les 3 à 7 heures.
- Business France est interrogé à chaque passage ; les trois banques au plus une
  fois par heure (l'heure de la dernière consultation est mémorisée), pour
  limiter le trafic sur leurs sites. Un lancement manuel (« Run workflow »)
  interroge toujours les banques.
- `seen_offers.json` mémorise les offres déjà envoyées (identifiants préfixés
  `sg_`, `bnp_`, `ntx_` pour les banques) ainsi que l'état de chaque source, et
  est commité par le workflow.
- Une fenêtre de 14 jours s'applique aux sources qui exposent une date de
  publication fiable (Business France, Société Générale, Natixis).

## Alerte en cas de panne d'une source

Si une source ne renvoie **aucune offre** (ou plante) depuis au moins **6 heures
et 3 passages d'affilée**, le bot envoie un mail « ⚠️ Bot VIE : … ne renvoie plus
d'offres » avec la dernière erreur, la date du dernier passage réussi, un lien
vers le site et un lien vers les logs GitHub. Une seule alerte est envoyée par
panne ; un mail « ✅ … fonctionne à nouveau » suit quand la source revient.

Les seuils se règlent dans `vie_bot_v2.py` (`HEALTH_ALERT_HOURS`,
`HEALTH_ALERT_MIN_CHECKS`).

## Secrets GitHub

| Nom | Valeur |
|---|---|
| `GMAIL_ADDRESS` | adresse Gmail expéditrice |
| `GMAIL_PASSWORD` | mot de passe d'application Gmail (16 caractères) |
| `RECIPIENT` | adresse de réception (facultatif) |

## Tester en local sans envoyer de mail

```bash
pip install requests beautifulsoup4 curl_cffi
DRY_RUN=1 PREVIEW_PATH=apercu.html python vie_bot_v2.py
```

`DRY_RUN=1` n'envoie rien, n'écrit pas `seen_offers.json` et dépose l'aperçu
HTML du mail dans `PREVIEW_PATH` (et celui d'une éventuelle alerte dans
`ALERT_PREVIEW_PATH`). Sans Playwright installé, Business France
renverra 0 offre en local : c'est attendu, il fonctionne depuis GitHub Actions.
