#!/usr/bin/env python3
"""
Bot de veille VIE — Business France + BNP Paribas + Société Générale

Récupère les offres VIE depuis 3 sources et envoie un email uniquement
lorsque de nouvelles offres apparaissent.

Variables d'environnement requises (GitHub Secrets) :
  GMAIL_ADDRESS  -> adresse Gmail expéditrice
  GMAIL_PASSWORD -> mot de passe d'application Gmail (16 caractères)
  RECIPIENT      -> adresse de réception (facultatif, défaut = GMAIL_ADDRESS)

Dépendances :
  pip install requests beautifulsoup4 playwright playwright-stealth
  playwright install chromium
"""

import hashlib
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("⚠️ Playwright non installé — BNP/SocGen seront ignorées.", file=sys.stderr)

try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False
    print("⚠️ playwright-stealth absent — scraping sans anti-détection.", file=sys.stderr)


# ── Configuration ──────────────────────────────────────────────────────────────

# Business France
BF_API_URL    = "https://civiweb-api-prd.azurewebsites.net/api/Offers/search"
BF_OFFER_BASE = "https://mon-vie-via.businessfrance.fr/offres"
BF_SITE       = "https://mon-vie-via.businessfrance.fr"
BF_SEARCH_URL = (
    "https://mon-vie-via.businessfrance.fr/offres/recherche"
    "?query&specializationsIds=19&geographicZones=2"
    "&geographicZones=3&geographicZones=4&teletravail=0&porteEnv=0"
)

BF_PAYLOAD = {
    "query": None,
    "specializationsIds": ["19"],        # Finance / Comptabilité / Gestion / Banque
    "geographicZones": ["2", "3", "4"],  # Amériques + Asie/Pacifique
    "teletravail": ["0"],
    "porteEnv": ["0"],
    "activitySectorId": [],
    "missionsTypesIds": [],
    "missionsDurations": [],
    "countriesIds": [],
    "studiesLevelId": [],
    "companiesSizes": [],
    "entreprisesIds": [0],
    "missionStartDate": None,
    "limit": 20,
}

# BNP Paribas
BNP_BASE_URL = "https://group.bnpparibas/emploi-carriere/toutes-offres-emploi/vie"
BNP_COLOR    = "#00965e"

# Société Générale
SG_URL   = "https://careers.societegenerale.com/en/search?refinementList[jobType][0]=COOPERATIVE"
SG_BASE  = "https://careers.societegenerale.com"
SG_COLOR = "#e30613"

# Général
LOOKBACK_DAYS = 14
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_offers.json")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
# On retire aussi les espaces internes : Google affiche le mot de passe
# d'application en 4 groupes de 4, et ils sont souvent copiés avec les espaces.
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "").replace(" ", "").replace("-", "").strip()
RECIPIENT     = os.environ.get("RECIPIENT", GMAIL_ADDRESS).strip()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# ⚠️ CORRECTIF PRINCIPAL
# L'API Business France renvoie 401 quand la requête ne ressemble pas à un appel
# XHR émis par le site lui-même. Ces en-têtes reproduisent exactement ceux que le
# navigateur envoie depuis mon-vie-via.businessfrance.fr.
BF_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Content-Type": "application/json",
    "Origin": BF_SITE,
    "Referer": BF_SITE + "/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "Connection": "keep-alive",
}


# ── Utilitaires ────────────────────────────────────────────────────────────────

def validate_env():
    if not GMAIL_ADDRESS:
        raise RuntimeError("Le secret GMAIL_ADDRESS est absent ou vide.")
    if not GMAIL_PASSWORD:
        raise RuntimeError("Le secret GMAIL_PASSWORD est absent ou vide.")
    if not RECIPIENT:
        raise RuntimeError("Le secret RECIPIENT est absent ou vide.")
    if len(GMAIL_PASSWORD) != 16:
        print(
            f"⚠️ GMAIL_PASSWORD fait {len(GMAIL_PASSWORD)} caractères après nettoyage "
            "(espaces et tirets retirés) ; un mot de passe d'application Gmail en fait 16.",
            file=sys.stderr,
        )


def load_seen() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {str(x) for x in data.get("seen_ids", [])}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"⚠️ Impossible de lire {SEEN_FILE}: {exc}", file=sys.stderr)
        return set()


def save_seen(ids: set):
    payload = {
        "seen_ids": sorted(str(x) for x in ids),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def send_email(subject: str, html_body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, RECIPIENT, msg.as_string())
        print("✅ Email envoyé.", file=sys.stderr)
    except smtplib.SMTPAuthenticationError as exc:
        print("❌ Gmail a refusé l'authentification SMTP.", file=sys.stderr)
        print(f"   Code SMTP : {exc.smtp_code}", file=sys.stderr)
        print(f"   Réponse   : {exc.smtp_error}", file=sys.stderr)
        raise


# ── Source 1 : Business France ─────────────────────────────────────────────────

def _bf_session() -> requests.Session:
    """
    Session pré-chauffée : on visite d'abord le site pour récupérer les cookies
    éventuels, ce qui rend l'appel API indiscernable d'un appel navigateur.
    """
    s = requests.Session()
    s.headers.update(BF_HEADERS)
    try:
        s.get(BF_SEARCH_URL, headers=HTTP_HEADERS, timeout=20)
    except requests.RequestException as exc:
        print(f"   [BF warn] préchauffage session impossible : {exc}", file=sys.stderr)
    return s


def fetch_bf_via_playwright():
    """
    ⚠️ MÉTHODE PRINCIPALE.

    L'API Business France renvoie 401 aux requêtes émises par `requests`, même
    avec des en-têtes de navigateur parfaits. Le pare-feu identifie la
    bibliothèque cliente à son empreinte TLS (JA3) — quelque chose que Python ne
    peut pas falsifier.

    Solution : ouvrir la page de recherche dans Chromium, puis exécuter le
    `fetch()` DEPUIS la page. La requête part alors du vrai navigateur, avec sa
    vraie empreinte TLS, ses vrais cookies et le bon Origin.
    """
    if not PLAYWRIGHT_AVAILABLE:
        print("   [BF] Playwright absent — repli sur requests.", file=sys.stderr)
        return None

    all_offers = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                locale="fr-FR",
                timezone_id="Europe/Paris",
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()

            if STEALTH_AVAILABLE:
                stealth_sync(page)

            print("   Ouverture de la page de recherche...", file=sys.stderr)
            page.goto(BF_SEARCH_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(4_000)

            # Bannière cookies éventuelle
            for sel in (
                "button:has-text('Tout accepter')",
                "button:has-text('Accepter')",
                "#onetrust-accept-btn-handler",
                "button:has-text('Accept all')",
            ):
                try:
                    btn = page.wait_for_selector(sel, timeout=2_500)
                    if btn:
                        btn.click()
                        print("   Bannière cookies acceptée.", file=sys.stderr)
                        page.wait_for_timeout(1_500)
                        break
                except Exception:
                    continue

            js = """
            async ({ url, payload }) => {
              const r = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
              });
              const text = await r.text();
              return { status: r.status, text };
            }
            """

            skip = 0
            while True:
                res = page.evaluate(
                    js,
                    {"url": BF_API_URL, "payload": {**BF_PAYLOAD, "skip": skip}},
                )
                status = res.get("status")

                if status != 200:
                    print(
                        f"   [BF playwright] skip={skip} → statut {status} : "
                        f"{str(res.get('text'))[:200]}",
                        file=sys.stderr,
                    )
                    break

                try:
                    data = json.loads(res["text"])
                except (json.JSONDecodeError, TypeError) as exc:
                    print(f"   [BF playwright] JSON illisible : {exc}", file=sys.stderr)
                    break

                total   = data.get("count", 0)
                results = data.get("result", [])

                known = {str(o.get("id")) for o in all_offers if o.get("id") is not None}
                for o in results:
                    if o.get("id") is not None and str(o["id"]) not in known:
                        all_offers.append(o)

                if len(all_offers) >= total or len(results) < BF_PAYLOAD["limit"]:
                    break

                skip += BF_PAYLOAD["limit"]
                page.wait_for_timeout(400)

            browser.close()

    except Exception as exc:
        print(f"   [BF playwright erreur] {exc}", file=sys.stderr)
        return all_offers or None

    if all_offers:
        print(
            f"   ✅ {len(all_offers)} offres récupérées via Chromium.",
            file=sys.stderr,
        )
    return all_offers or None


def fetch_bf_via_requests():
    """Repli : appel direct avec requests (échoue si le WAF filtre l'empreinte TLS)."""
    all_offers, skip = [], 0
    session = _bf_session()

    while True:
        resp = None
        # 3 tentatives : l'API renvoie parfois un 401/403 transitoire.
        for attempt in range(3):
            try:
                resp = session.post(
                    BF_API_URL,
                    json={**BF_PAYLOAD, "skip": skip},
                    timeout=30,
                )
                if resp.status_code in (401, 403, 429) and attempt < 2:
                    print(
                        f"   [BF retry {attempt + 1}/3] statut {resp.status_code} "
                        f"sur skip={skip}",
                        file=sys.stderr,
                    )
                    time.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == 2:
                    print(f"[BF erreur skip={skip}] {exc}", file=sys.stderr)
                    return all_offers
                time.sleep(3 * (attempt + 1))
        else:
            return all_offers

        if resp is None:
            return all_offers

        data    = resp.json()
        total   = data.get("count", 0)
        results = data.get("result", [])

        seen = {str(o.get("id")) for o in all_offers if o.get("id") is not None}
        for o in results:
            if o.get("id") is not None and str(o["id"]) not in seen:
                all_offers.append(o)

        if len(all_offers) >= total or len(results) < BF_PAYLOAD["limit"]:
            break

        skip += BF_PAYLOAD["limit"]

    return all_offers


def fetch_bf_all():
    """Chromium d'abord (contourne le filtrage TLS), requests en repli."""
    offers = fetch_bf_via_playwright()
    if offers:
        return offers
    print("   Repli sur requests...", file=sys.stderr)
    return fetch_bf_via_requests()


def _parse_bf_date(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "")).date()
    except ValueError:
        return None


def fmt_bf_offer(offer: dict) -> dict:
    oid   = offer.get("id")
    start = offer.get("missionStartDate", "")
    try:
        start_str = (
            datetime.fromisoformat(start.replace("Z", "")).strftime("%B %Y")
            if start else "Non précisé"
        )
    except ValueError:
        start_str = start[:7] if start else "Non précisé"

    ind     = offer.get("indemnite")
    ind_str = f"{ind:,.0f} €/mois".replace(",", " ") if ind else "Non précisée"

    return {
        "uid":       str(oid),
        "title":     offer.get("missionTitle", "Sans titre"),
        "company":   offer.get("organizationName", "?"),
        "location":  f"{offer.get('cityName', '?')}, {offer.get('countryName', '?')}",
        "duration":  f"{offer.get('missionDuration', '?')} mois",
        "start":     start_str,
        "indemnite": ind_str,
        "url":       f"{BF_OFFER_BASE}/{oid}",
        "source":    "Business France",
        "color":     "#1a3c6e",
    }


def get_bf_new(seen_ids: set) -> list:
    print("📡 Business France...", file=sys.stderr)
    try:
        all_offers = fetch_bf_all()
        print(f"   {len(all_offers)} offres récupérées.", file=sys.stderr)

        if not all_offers:
            print(
                "   ⚠️ Aucune offre récupérée, y compris via Chromium. "
                "Le blocage est alors basé sur l'adresse IP du runner GitHub "
                "Actions : il faut héberger le bot ailleurs.",
                file=sys.stderr,
            )
            return []

        cutoff = datetime.now(timezone.utc).date() - timedelta(days=LOOKBACK_DAYS - 1)
        recent = [
            o for o in all_offers
            if (_parse_bf_date(o.get("startBroadcastDate")) or date.min) >= cutoff
        ]
        print(
            f"   {len(recent)} offre(s) diffusée(s) sur les {LOOKBACK_DAYS} derniers jours.",
            file=sys.stderr,
        )

        new = [o for o in recent if str(o.get("id")) not in seen_ids]
        print(f"   {len(new)} nouvelle(s) offre(s).", file=sys.stderr)
        return [fmt_bf_offer(o) for o in new]
    except Exception as exc:
        print(f"[BF erreur] {exc}", file=sys.stderr)
        return []


# ── Source 2 : BNP Paribas ─────────────────────────────────────────────────────

def get_bnp_new(seen_ids: set) -> list:
    """
    BNP Paribas utilise Akamai EdgeSuite qui bloque systématiquement les plages
    IP des serveurs GitHub Actions (Microsoft Azure). Ce blocage est décidé côté
    réseau avant même l'évaluation du fingerprint navigateur — aucun contournement
    logiciel n'est possible depuis GitHub Actions.

    → Les offres BNP disponibles sur Business France sont déjà remontées par la
      source Business France. On logue un avertissement et on retourne 0 offres.
    """
    print("📡 BNP Paribas...", file=sys.stderr)
    print(
        "   ⚠️  BNP Paribas (group.bnpparibas) bloque les IP GitHub Actions via "
        "Akamai EdgeSuite — source ignorée. Les offres BNP publiées sur Business "
        "France restent surveillées par la source Business France.",
        file=sys.stderr,
    )
    print("   0 nouvelle(s) offre(s) BNP.", file=sys.stderr)
    return []


# ── Source 3 : Société Générale ────────────────────────────────────────────────

def get_sg_new(seen_ids: set) -> list:
    """
    Récupère les offres VIE Société Générale via l'API sg-careers-offers.

    Stratégie :
      1. Charger la page avec Playwright et intercepter les RÉPONSES réseau.
      2. Capturer le token depuis /sg-careers-offers/get-token (réponse JSON).
      3. Capturer la réponse de l'appel de recherche qui suit.
      4. Si le token est capturé mais pas les résultats, tenter des endpoints
         courants avec requests (GET puis POST) en passant le token + cookies.
    """
    print("📡 Société Générale...", file=sys.stderr)

    if not PLAYWRIGHT_AVAILABLE:
        print("   Playwright absent — source ignorée.", file=sys.stderr)
        return []

    captured: dict = {}   # clés : "token", "search", "search_url"

    # ── Étape 1 : Playwright — intercepter les réponses API ───────────────────
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx  = browser.new_context(user_agent=USER_AGENT)
            page = ctx.new_page()

            if STEALTH_AVAILABLE:
                stealth_sync(page)

            def _on_response(resp):
                url = resp.url
                if "sg-careers-offers" not in url:
                    return
                try:
                    status = resp.status
                    print(
                        f"   [SG api] {resp.request.method} {url[:120]} → {status}",
                        file=sys.stderr,
                    )
                    if status != 200:
                        return
                    data = resp.json()
                except Exception:
                    return

                # ── Endpoint token ────────────────────────────────────────────
                if "get-token" in url:
                    tok = None
                    if isinstance(data, str) and len(data) > 10:
                        tok = data
                    elif isinstance(data, dict):
                        for k in ("token", "access_token", "accessToken",
                                  "jwt", "authToken", "value", "bearerToken"):
                            if data.get(k):
                                tok = data[k]
                                break
                        if not tok:
                            print(
                                f"   [SG debug] clés token response: {list(data.keys())}",
                                file=sys.stderr,
                            )
                    if tok:
                        captured["token"] = tok
                        print(f"   ✅ Token capturé ({len(tok)} chars)", file=sys.stderr)
                    return

                # ── Autres endpoints : tenter d'extraire les offres ───────────
                if "search" in captured:
                    return

                if isinstance(data, dict):
                    print(
                        f"   [SG debug] {url.split('/')[-1].split('?')[0]} "
                        f"clés: {list(data.keys())[:12]}",
                        file=sys.stderr,
                    )
                    for key in ("hits", "results", "offers", "jobs", "data",
                                "items", "content", "jobOffers", "jobList"):
                        val = data.get(key)
                        if isinstance(val, list) and val:
                            captured["search"] = val
                            captured["search_url"] = url
                            print(
                                f"   ✅ {len(val)} offres trouvées (clé '{key}')",
                                file=sys.stderr,
                            )
                            return
                    for k in ("_embedded", "embedded"):
                        emb = data.get(k)
                        if isinstance(emb, dict):
                            for sub in emb.values():
                                if isinstance(sub, list) and sub:
                                    captured["search"] = sub
                                    captured["search_url"] = url
                                    print(
                                        f"   ✅ {len(sub)} offres (embedded '{k}')",
                                        file=sys.stderr,
                                    )
                                    return
                elif isinstance(data, list) and data:
                    captured["search"] = data
                    captured["search_url"] = url
                    print(f"   ✅ {len(data)} offres (liste directe)", file=sys.stderr)

            page.on("response", _on_response)

            print(f"   Chargement de {SG_URL} ...", file=sys.stderr)
            page.goto(SG_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3_000)

            # Accepter la bannière cookies
            cookie_accepted = False
            for ck_sel in [
                "button:has-text('Accept all')",
                "button:has-text('Tout accepter')",
                "button:has-text('Accept All')",
                "button:has-text('Accepter tout')",
                "button:has-text('Accepter')",
                "button:has-text('Accept')",
            ]:
                try:
                    btn = page.wait_for_selector(ck_sel, timeout=4_000)
                    if btn:
                        btn.click()
                        print("   Bannière cookies acceptée.", file=sys.stderr)
                        cookie_accepted = True
                        page.wait_for_timeout(2_000)
                        break
                except Exception:
                    continue

            if cookie_accepted or "search" not in captured:
                print("   Rechargement pour déclencher l'API...", file=sys.stderr)
                page.goto(SG_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(10_000)

            # ── Étape 2 : token capturé mais pas les résultats → appel direct ──
            if "token" in captured and "search" not in captured:
                print(
                    "   Token capturé mais pas de résultats — tentative appel API direct...",
                    file=sys.stderr,
                )
                browser_cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                token = captured["token"]
                api_hdrs = {
                    "Authorization":    f"Bearer {token}",
                    "Accept":           "application/json",
                    "Content-Type":     "application/json",
                    "User-Agent":       USER_AGENT,
                    "Referer":          SG_URL,
                    "Origin":           SG_BASE,
                    "X-Requested-With": "XMLHttpRequest",
                }

                get_endpoints = [
                    ("/sg-careers-offers/search",
                     {"jobType": "COOPERATIVE", "size": 200, "page": 0}),
                    ("/sg-careers-offers/offers",
                     {"jobType": "COOPERATIVE", "size": 200}),
                    ("/sg-careers-offers/jobs",
                     {"jobType": "COOPERATIVE", "size": 200}),
                    ("/sg-careers-offers/get-offers",
                     {"jobType": "COOPERATIVE", "size": 200}),
                    ("/sg-careers-offers/cooperative",
                     {"size": 200}),
                ]
                for ep_path, params in get_endpoints:
                    ep = SG_BASE + ep_path
                    try:
                        r = requests.get(
                            ep, headers=api_hdrs, params=params,
                            cookies=browser_cookies, timeout=20,
                        )
                        print(
                            f"   [SG debug] GET {ep_path} → {r.status_code}",
                            file=sys.stderr,
                        )
                        if r.status_code == 200:
                            _extract_search(r.json(), captured, ep)
                            if "search" in captured:
                                break
                    except Exception as e:
                        print(f"   [SG debug] GET {ep_path}: {e}", file=sys.stderr)

                if "search" not in captured:
                    post_endpoints = [
                        ("/sg-careers-offers/search",
                         {"jobType": "COOPERATIVE", "size": 200, "page": 0}),
                        ("/sg-careers-offers/query",
                         {"jobType": "COOPERATIVE", "limit": 200}),
                    ]
                    for ep_path, body in post_endpoints:
                        ep = SG_BASE + ep_path
                        try:
                            r = requests.post(
                                ep, json=body, headers=api_hdrs,
                                cookies=browser_cookies, timeout=20,
                            )
                            print(
                                f"   [SG debug] POST {ep_path} → {r.status_code}",
                                file=sys.stderr,
                            )
                            if r.status_code == 200:
                                _extract_search(r.json(), captured, ep)
                                if "search" in captured:
                                    break
                        except Exception as e:
                            print(f"   [SG debug] POST {ep_path}: {e}", file=sys.stderr)

            browser.close()

    except Exception as exc:
        print(f"[SG erreur Playwright] {exc}", file=sys.stderr)

    # ── Étape 3 : parser les offres capturées ─────────────────────────────────
    if "search" not in captured:
        print(
            "[SG] Impossible de récupérer les offres SG — source ignorée.",
            file=sys.stderr,
        )
        print("   0 nouvelle(s) offre(s) SG.", file=sys.stderr)
        return []

    items = captured["search"]
    print(f"   Parsing de {len(items)} offres...", file=sys.stderr)
    if items and isinstance(items[0], dict):
        print(
            f"   [SG debug] clés offre: {list(items[0].keys())[:15]}",
            file=sys.stderr,
        )

    new_offers = []
    for item in items:
        if not isinstance(item, dict):
            continue

        job_type = (
            item.get("jobType") or item.get("type") or item.get("contractType") or ""
        ).upper()
        if job_type and "COOPERATIVE" not in job_type and "VIE" not in job_type:
            continue

        title = (
            item.get("title") or item.get("jobTitle") or item.get("name")
            or item.get("label") or item.get("offerTitle") or ""
        ).strip()
        if not title:
            continue

        href = (
            item.get("url") or item.get("link") or item.get("applyUrl")
            or item.get("jobUrl") or item.get("offerUrl") or ""
        ).strip()
        if href and not href.startswith("http"):
            href = SG_BASE + href
        if not href:
            oid = (
                item.get("id") or item.get("objectID") or item.get("jobId")
                or item.get("reference") or item.get("offerId")
            )
            if oid:
                href = f"{SG_BASE}/en/offer/{oid}"

        location = (
            item.get("location") or item.get("city") or item.get("country")
            or item.get("place") or item.get("locationLabel") or item.get("jobLocation") or ""
        ).strip()

        oid = (
            item.get("id") or item.get("objectID") or item.get("jobId")
            or item.get("reference") or item.get("offerId")
        )
        uid = f"sg_{oid}" if oid else f"sg_{hashlib.md5(title.encode()).hexdigest()[:12]}"

        if uid not in seen_ids:
            new_offers.append({
                "uid":      uid,
                "title":    title,
                "company":  "Société Générale",
                "location": location,
                "url":      href or SG_URL,
                "source":   "Société Générale",
                "color":    SG_COLOR,
            })

    print(f"   {len(new_offers)} nouvelle(s) offre(s) SG.", file=sys.stderr)
    return new_offers


def _extract_search(data, captured: dict, url: str):
    """Tente d'extraire une liste d'offres depuis une réponse JSON API."""
    if isinstance(data, dict):
        print(f"   [SG debug] clés: {list(data.keys())[:12]}", file=sys.stderr)
        for key in ("hits", "results", "offers", "jobs", "data",
                    "items", "content", "jobOffers", "jobList"):
            val = data.get(key)
            if isinstance(val, list) and val:
                captured["search"] = val
                captured["search_url"] = url
                print(
                    f"   ✅ {len(val)} offres via API direct (clé '{key}')",
                    file=sys.stderr,
                )
                return
        for k in ("_embedded", "embedded"):
            emb = data.get(k)
            if isinstance(emb, dict):
                for sub in emb.values():
                    if isinstance(sub, list) and sub:
                        captured["search"] = sub
                        captured["search_url"] = url
                        print(f"   ✅ {len(sub)} offres via embedded", file=sys.stderr)
                        return
    elif isinstance(data, list) and data:
        captured["search"] = data
        captured["search_url"] = url
        print(f"   ✅ {len(data)} offres (liste directe)", file=sys.stderr)


# ── Email ──────────────────────────────────────────────────────────────────────

def _offer_row(offer: dict) -> str:
    color    = offer.get("color", "#1a3c6e")
    source   = offer.get("source", "")
    company  = offer.get("company", "")
    loc      = offer.get("location", "")
    duration = offer.get("duration", "")
    start    = offer.get("start", "")
    indem    = offer.get("indemnite", "")

    parts = []
    if loc:      parts.append(f"📍 {loc}")
    if duration: parts.append(f"⏱ {duration}")
    if start:    parts.append(f"🗓 {start}")
    if indem:    parts.append(f"💶 {indem}")
    meta = " &nbsp;·&nbsp; ".join(parts)

    badge = (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:11px;font-weight:600">{source}</span>'
    )

    return f"""
      <tr>
        <td style="padding:14px 0;border-bottom:1px solid #eee">
          {badge}
          <a href="{offer['url']}"
             style="display:block;font-size:15px;font-weight:700;color:#1a3c6e;
                    text-decoration:none;margin-top:5px">{offer['title']}</a>
          <span style="font-size:14px;color:#333;font-weight:600">{company}</span>
          {"<div style='margin-top:6px;font-size:13px;color:#666'>" + meta + "</div>" if meta else ""}
          <div style="margin-top:8px">
            <a href="{offer['url']}"
               style="background:{color};color:#fff;padding:6px 16px;
                      border-radius:4px;font-size:12px;text-decoration:none;
                      font-weight:600">Voir l'offre →</a>
          </div>
        </td>
      </tr>"""


def build_html(offers: list, counts: dict) -> str:
    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
    total   = len(offers)
    rows    = "".join(_offer_row(o) for o in offers)

    summary_parts = []
    for src, cnt in counts.items():
        if cnt > 0:
            icons = {"Business France": "🏛", "BNP Paribas": "🟢", "Société Générale": "🔴"}
            summary_parts.append(f"{icons.get(src, '')} {cnt} {src}")
    summary = " &nbsp;·&nbsp; ".join(summary_parts)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:30px 0">
  <tr><td align="center">
    <table width="640" cellpadding="0" cellspacing="0"
           style="background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.08)">
      <!-- Header -->
      <tr><td style="background:#1a3c6e;padding:22px 30px;border-radius:8px 8px 0 0">
        <h2 style="margin:0;color:#fff;font-size:19px">🆕 Nouvelle(s) offre(s) VIE détectée(s)</h2>
        <p style="margin:6px 0 0;color:#aec6e8;font-size:13px">
          {total} nouvelle(s) le {now_str}
        </p>
        <p style="margin:4px 0 0;color:#aec6e8;font-size:12px">{summary}</p>
      </td></tr>
      <!-- Offers -->
      <tr><td style="padding:8px 30px 20px">
        <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
      </td></tr>
      <!-- Footer -->
      <tr><td style="background:#f4f6f9;padding:14px 30px;text-align:center;
                     border-radius:0 0 8px 8px">
        <a href="{BF_SEARCH_URL}"
           style="color:#1a3c6e;font-size:13px;font-weight:600;text-decoration:none">
          Voir toutes les offres Business France →
        </a>
        <p style="margin:6px 0 0;font-size:11px;color:#999">
          Sources : Business France &nbsp;·&nbsp; BNP Paribas &nbsp;·&nbsp; Société Générale
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    validate_env()

    seen_ids = load_seen()
    counts   = {"Business France": 0, "BNP Paribas": 0, "Société Générale": 0}
    all_new  = []
    all_seen_to_save = set(seen_ids)

    # ── Business France
    bf_new = get_bf_new(seen_ids)
    counts["Business France"] = len(bf_new)
    all_new.extend(bf_new)
    for o in bf_new:
        all_seen_to_save.add(o["uid"])

    # ── BNP Paribas
    bnp_new = get_bnp_new(seen_ids)
    counts["BNP Paribas"] = len(bnp_new)
    all_new.extend(bnp_new)
    for o in bnp_new:
        all_seen_to_save.add(o["uid"])

    # ── Société Générale
    sg_new = get_sg_new(seen_ids)
    counts["Société Générale"] = len(sg_new)
    all_new.extend(sg_new)
    for o in sg_new:
        all_seen_to_save.add(o["uid"])

    # ── Bilan
    total = len(all_new)
    print(f"\n📊 Total nouvelles offres : {total}", file=sys.stderr)
    for src, cnt in counts.items():
        print(f"   {src} : {cnt}", file=sys.stderr)

    if not all_new:
        print("Aucune nouvelle offre. Pas d'email envoyé.", file=sys.stderr)
        save_seen(all_seen_to_save)
        return

    html    = build_html(all_new, counts)
    subject = (
        f"🆕 {total} nouvelle(s) offre(s) VIE"
        f" — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )

    send_email(subject, html)
    save_seen(all_seen_to_save)
    print("💾 Historique mis à jour.", file=sys.stderr)


if __name__ == "__main__":
    main()
