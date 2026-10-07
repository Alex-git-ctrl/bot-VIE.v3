#!/usr/bin/env python3
"""
Bot de veille VIE — Business France + Société Générale + BNP Paribas + Natixis

Récupère les offres VIE depuis 4 sources et envoie un email uniquement
lorsque de nouvelles offres apparaissent.

Sources et méthodes :
  • Business France  : API Civiweb, appelée depuis Chromium (Playwright) car
                       l'API rejette les clients non-navigateur (empreinte TLS).
  • Société Générale : moteur de recherche interne « Quantum », appelé via le
                       proxy du site (get-token puis search-proxy.php) avec
                       curl_cffi pour imiter l'empreinte TLS de Chrome (Imperva).
  • BNP Paribas      : liste VIE rendue côté serveur, lue avec curl_cffi
                       (Akamai bloque requests et Chromium headless).
  • Natixis          : API REST du portail recrutement Groupe BPCE (requests).
                       Inclut Natixis IM, AEW, Ostrum, Mirova…

Variables d'environnement requises (GitHub Secrets) :
  GMAIL_ADDRESS  -> adresse Gmail expéditrice
  GMAIL_PASSWORD -> mot de passe d'application Gmail (16 caractères)
  RECIPIENT      -> adresse de réception (facultatif, défaut = GMAIL_ADDRESS)

Variables facultatives :
  DRY_RUN=1      -> pas d'email, pas d'écriture de seen_offers.json ;
                    l'aperçu HTML est écrit dans PREVIEW_PATH (défaut apercu_mail.html)
  FORCE_BANKS=1  -> interroge les banques même hors du créneau horaire

Dépendances :
  pip install requests beautifulsoup4 curl_cffi playwright tf-playwright-stealth
  playwright install chromium
"""

import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("⚠️ Playwright non installé — Business France passera par requests.", file=sys.stderr)

try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    print(
        "⚠️ curl_cffi absent — Société Générale et BNP Paribas seront ignorées.",
        file=sys.stderr,
    )


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
BF_COLOR = "#1a3c6e"

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

# Société Générale — moteur de recherche « Quantum » appelé via le proxy du site.
# Les noms de champs viennent de themes/custom/sg_careers/js/quantum/global-quantum.js
SG_BASE        = "https://careers.societegenerale.com"
SG_CSRF_PAGES  = (SG_BASE + "/en/jobs", SG_BASE + "/en")   # pages fournissant cookies + csrfToken
SG_TOKEN_URL   = SG_BASE + "/sg-careers-offers/get-token"
SG_PROXY_URL   = SG_BASE + "/search-proxy.php"
SG_SEARCH_PAGE = SG_BASE + "/en/search?refinementList%5BjobType%5D%5B0%5D=COOPERATIVE"
SG_QUANTUM_BASE_DEFAULT = (
    "https://api.socgen.com/business-support/it-for-it-support/"
    "cognitive-service-knowledge/api/v1"
)
SG_COLOR          = "#e30613"
SG_PAGE_SIZE      = 50
SG_FIELD_DOCTYPE  = "sourcestr6"      # "job" / "page"
SG_FIELD_CONTRACT = "sourcestr8"      # COOPERATIVE = V.I.E
SG_FIELD_LOCATION = "sourcestr7"      # "Luxembourg, Luxembourg"
SG_FIELD_ENTITY   = "sourcestr15"     # "SG CIB", "SG Luxembourg"…
SG_FIELD_FAMILY   = "sourcestr10"     # "Finance", "Private Banking"…
SG_FIELD_REF      = "sourcestr4"      # "26000KYE"
SG_FIELD_PUBLISHED = "sourcedatetime1"

# BNP Paribas — liste VIE rendue côté serveur, 10 offres par page.
BNP_BASE      = "https://group.bnpparibas"
BNP_LIST_URL  = BNP_BASE + "/emploi-carriere/toutes-offres-emploi/vie"
BNP_COLOR     = "#00965e"
BNP_MAX_PAGES = 5

# Natixis — portail recrutement du Groupe BPCE (WordPress headless).
NTX_BASE      = "https://recrutement.natixis.com"
NTX_API_URL   = NTX_BASE + "/app/wp-json/bpce/v1/search/jobs/"
NTX_SEARCH_PAGE = NTX_BASE + "/nos-offres-demploi"
NTX_COLOR     = "#5f259f"
NTX_PAGE_SIZE = 100

# Ordre des sections du mail : (nom, couleur, icône, lien « voir toutes les offres »)
SOURCES = (
    ("Business France",  BF_COLOR,  "🏛", BF_SEARCH_URL),
    ("Société Générale", SG_COLOR,  "🔴", SG_SEARCH_PAGE),
    ("BNP Paribas",      BNP_COLOR, "🟢", BNP_LIST_URL),
    ("Natixis",          NTX_COLOR, "🟣", NTX_SEARCH_PAGE),
)

# Général
LOOKBACK_DAYS = 14
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_offers.json")

# Les banques ne sont interrogées qu'une fois par heure (premier passage du cron,
# minute 7) ; Business France l'est à chaque passage.
BANKS_WINDOW_MINUTES = 15

DRY_RUN     = os.environ.get("DRY_RUN", "") == "1"
FORCE_BANKS = os.environ.get("FORCE_BANKS", "") == "1"
PREVIEW_PATH = os.environ.get("PREVIEW_PATH", "apercu_mail.html")

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

# L'API Business France renvoie 401 quand la requête ne ressemble pas à un appel
# XHR émis par le site lui-même. Ces en-têtes reproduisent ceux du navigateur.
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

JSON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}


# ── Utilitaires ────────────────────────────────────────────────────────────────

def log(msg: str):
    print(msg, file=sys.stderr)


def validate_env():
    if not GMAIL_ADDRESS:
        raise RuntimeError("Le secret GMAIL_ADDRESS est absent ou vide.")
    if not GMAIL_PASSWORD:
        raise RuntimeError("Le secret GMAIL_PASSWORD est absent ou vide.")
    if not RECIPIENT:
        raise RuntimeError("Le secret RECIPIENT est absent ou vide.")
    if len(GMAIL_PASSWORD) != 16:
        log(
            f"⚠️ GMAIL_PASSWORD fait {len(GMAIL_PASSWORD)} caractères après nettoyage "
            "(espaces et tirets retirés) ; un mot de passe d'application Gmail en fait 16."
        )


def load_seen() -> set:
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {str(x) for x in data.get("seen_ids", [])}
    except (json.JSONDecodeError, OSError) as exc:
        log(f"⚠️ Impossible de lire {SEEN_FILE}: {exc}")
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
        log("✅ Email envoyé.")
    except smtplib.SMTPAuthenticationError as exc:
        log("❌ Gmail a refusé l'authentification SMTP.")
        log(f"   Code SMTP : {exc.smtp_code}")
        log(f"   Réponse   : {exc.smtp_error}")
        raise


def retry(fn, label: str, attempts: int = 3):
    """Exécute fn() jusqu'à `attempts` fois avec un délai croissant entre les essais."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == attempts:
                raise
            log(f"   [{label} tentative {attempt}/{attempts}] {exc} — nouvel essai…")
            time.sleep(3 * attempt)


def parse_date(raw) -> "date | None":
    """Accepte '2026-10-07', '2026-10-07 14:00:00', '2026-10-07T14:00:00Z'…"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "").strip()[:19]).date()
    except ValueError:
        return None


def fmt_date(d) -> str:
    return d.strftime("%d/%m/%Y") if d else ""


def lookback_cutoff() -> date:
    return datetime.now(timezone.utc).date() - timedelta(days=LOOKBACK_DAYS - 1)


def clean_text(s) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(s or ""))).strip()


def banks_due() -> bool:
    if DRY_RUN or FORCE_BANKS:
        return True
    return datetime.now(timezone.utc).minute < BANKS_WINDOW_MINUTES


def unique_by_uid(offers) -> list:
    """Garde la première occurrence de chaque uid (ordre préservé)."""
    seen, out = set(), []
    for o in offers:
        if o["uid"] not in seen:
            seen.add(o["uid"])
            out.append(o)
    return out


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
        log(f"   [BF warn] préchauffage session impossible : {exc}")
    return s


def fetch_bf_via_playwright():
    """
    MÉTHODE PRINCIPALE.

    L'API Business France renvoie 401 aux requêtes émises par `requests` (et par
    curl_cffi depuis GitHub Actions), même avec des en-têtes parfaits : le
    pare-feu identifie la bibliothèque cliente à son empreinte TLS.

    Solution : ouvrir la page de recherche dans Chromium, puis exécuter le
    `fetch()` DEPUIS la page. La requête part alors du vrai navigateur, avec sa
    vraie empreinte TLS, ses vrais cookies et le bon Origin.
    """
    if not PLAYWRIGHT_AVAILABLE:
        log("   [BF] Playwright absent — repli sur requests.")
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

            log("   Ouverture de la page de recherche…")
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
                        log("   Bannière cookies acceptée.")
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
                    log(
                        f"   [BF playwright] skip={skip} → statut {status} : "
                        f"{str(res.get('text'))[:200]}"
                    )
                    break

                try:
                    data = json.loads(res["text"])
                except (json.JSONDecodeError, TypeError) as exc:
                    log(f"   [BF playwright] JSON illisible : {exc}")
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
        log(f"   [BF playwright erreur] {exc}")
        return all_offers or None

    if all_offers:
        log(f"   ✅ {len(all_offers)} offres récupérées via Chromium.")
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
                    log(f"   [BF retry {attempt + 1}/3] statut {resp.status_code} sur skip={skip}")
                    time.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == 2:
                    log(f"[BF erreur skip={skip}] {exc}")
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
    log("   Repli sur requests…")
    return fetch_bf_via_requests()


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
    ind_str = f"{ind:,.0f} €/mois".replace(",", " ") if ind else "Non précisée"

    return {
        "uid":       str(oid),
        "title":     offer.get("missionTitle", "Sans titre"),
        "company":   offer.get("organizationName", "?"),
        "location":  f"{offer.get('cityName', '?')}, {offer.get('countryName', '?')}",
        "duration":  f"{offer.get('missionDuration', '?')} mois",
        "start":     start_str,
        "indemnite": ind_str,
        "published": parse_date(offer.get("startBroadcastDate")),
        "url":       f"{BF_OFFER_BASE}/{oid}",
        "source":    "Business France",
        "color":     BF_COLOR,
    }


def get_bf_new(seen_ids: set):
    """Retourne (nouvelles offres, toutes les offres récupérées) au format commun."""
    log("📡 Business France…")
    try:
        all_offers = fetch_bf_all()
        log(f"   {len(all_offers)} offres récupérées.")

        if not all_offers:
            if PLAYWRIGHT_AVAILABLE:
                log(
                    "   ⚠️ Aucune offre récupérée, y compris via Chromium. "
                    "Le blocage est alors basé sur l'adresse IP du runner GitHub "
                    "Actions : il faut héberger le bot ailleurs."
                )
            else:
                log("   ⚠️ Aucune offre : l'API Business France exige Chromium (Playwright).")
            return [], []

        cutoff = lookback_cutoff()
        recent = [
            o for o in all_offers
            if (parse_date(o.get("startBroadcastDate")) or date.min) >= cutoff
        ]
        log(f"   {len(recent)} offre(s) diffusée(s) sur les {LOOKBACK_DAYS} derniers jours.")

        new = [o for o in recent if str(o.get("id")) not in seen_ids]
        log(f"   {len(new)} nouvelle(s) offre(s).")
        return [fmt_bf_offer(o) for o in new], [fmt_bf_offer(o) for o in all_offers]
    except Exception as exc:
        log(f"[BF erreur] {exc}")
        return [], []


# ── Source 2 : Société Générale ────────────────────────────────────────────────

def fetch_sg_docs() -> list:
    """
    Reproduit les appels que fait la page de recherche du site :
      1. une page du site  → cookies Imperva + drupalSettings.csrfToken (+ base_url Quantum)
      2. GET  /sg-careers-offers/get-token   → JWT (valable ~1 h)
      3. POST /search-proxy.php              → relaie vers Quantum /search-profile
    Le filtre contrat COOPERATIVE correspond à « V.I.E » dans les facettes du site.
    """
    s = curl_requests.Session(impersonate="chrome", timeout=40)
    s.headers.update({"Accept-Language": "en-US,en;q=0.9,fr;q=0.8"})

    csrf, quantum_base = None, SG_QUANTUM_BASE_DEFAULT
    for page_url in SG_CSRF_PAGES:
        r = s.get(page_url)
        if r.status_code != 200:
            log(f"   [SG] {page_url} → HTTP {r.status_code}")
            continue
        m_csrf = re.search(r'"csrfToken":"([0-9a-f]+)"', r.text)
        m_base = re.search(r'"base_url":"([^"]+)"', r.text)
        if m_base:
            quantum_base = m_base.group(1).replace("\\/", "/")
        if m_csrf:
            csrf = m_csrf.group(1)
            break
    if not csrf:
        raise RuntimeError("csrfToken introuvable dans drupalSettings (challenge Imperva ?)")

    t = s.get(
        SG_TOKEN_URL,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": csrf,
            "Referer": SG_SEARCH_PAGE,
            "Accept": "application/json, text/plain, */*",
        },
    )
    if t.status_code != 200:
        raise RuntimeError(f"get-token → HTTP {t.status_code} : {t.text[:120]}")
    token = t.json().get("token")
    if not token:
        raise RuntimeError("get-token : réponse sans token")

    proxy_headers = {
        "Content-Type": "application/json",
        "Authorization-API": f"Bearer {token}",
        "X-Proxy-URL": quantum_base + "/search-profile",
        "Referer": SG_SEARCH_PAGE,
        "Origin": SG_BASE,
        "Accept": "application/json, text/plain, */*",
    }

    docs, skip_from = [], 0
    while True:
        body = {
            "profile": "ces_profile_sgcareers",
            "query": {
                "advanced": [
                    {"type": "simple", "name": SG_FIELD_DOCTYPE,  "op": "eq", "value": "job"},
                    {"type": "multi",  "name": SG_FIELD_CONTRACT, "op": "eq", "values": ["COOPERATIVE"]},
                ],
                "skipCount": SG_PAGE_SIZE,
                "skipFrom": skip_from,
                "sort": "sourcedatetime1.desc",
            },
            "lang": "en",
            "responseType": "SearchResult",
        }
        p = s.post(SG_PROXY_URL, json=body, headers=proxy_headers)
        if p.status_code != 200:
            raise RuntimeError(f"search-proxy → HTTP {p.status_code} : {p.text[:120]}")
        data = p.json()
        page_docs = (data.get("Result") or {}).get("Docs") or []
        docs.extend(page_docs)
        total = data.get("TotalCount") or 0
        if not page_docs or len(docs) >= total:
            break
        skip_from += SG_PAGE_SIZE
        time.sleep(1)
    return docs


def fmt_sg_offer(doc: dict) -> dict:
    ref = clean_text(doc.get(SG_FIELD_REF)) or clean_text(doc.get("sourcestr12"))
    title = clean_text(doc.get("title") or doc.get("resulttitle"))
    uid = f"sg_{ref}" if ref else f"sg_{hashlib.md5(title.encode()).hexdigest()[:12]}"
    entity = clean_text(doc.get(SG_FIELD_ENTITY))
    company = "Société Générale"
    if entity and entity.lower() not in ("societe generale", "société générale"):
        company += f" — {entity}"
    return {
        "uid":       uid,
        "title":     title or "Sans titre",
        "company":   company,
        "location":  clean_text(doc.get(SG_FIELD_LOCATION)),
        "detail":    clean_text(doc.get(SG_FIELD_FAMILY)),
        "published": parse_date(doc.get(SG_FIELD_PUBLISHED)),
        "url":       doc.get("resulturl") or doc.get("url1") or SG_SEARCH_PAGE,
        "source":    "Société Générale",
        "color":     SG_COLOR,
    }


def get_sg_new(seen_ids: set) -> list:
    log("📡 Société Générale…")
    if not CURL_CFFI_AVAILABLE:
        log("   curl_cffi absent — source ignorée.")
        return []
    try:
        docs = retry(fetch_sg_docs, "SG")
        log(f"   {len(docs)} offre(s) V.I.E en ligne.")
        # L'index contient parfois une variante FR et une variante EN d'une même
        # référence : on garde la version anglaise (dédoublonnage par uid ensuite).
        docs = sorted(
            (d for d in docs if isinstance(d, dict)),
            key=lambda d: 0 if (d.get("languages") or "en") == "en" else 1,
        )
        offers = unique_by_uid(fmt_sg_offer(d) for d in docs)
        cutoff = lookback_cutoff()
        recent = [o for o in offers if o["published"] is None or o["published"] >= cutoff]
        new = [o for o in recent if o["uid"] not in seen_ids]
        log(f"   {len(new)} nouvelle(s) offre(s).")
        return new
    except Exception as exc:
        log(f"[SG erreur] {exc}")
        return []


# ── Source 3 : BNP Paribas ─────────────────────────────────────────────────────

def fetch_bnp_cards() -> list:
    """
    Lit la liste VIE (HTML rendu côté serveur). Akamai Bot Manager refuse
    `requests` (403) et Chromium headless (Access Denied) mais accepte une
    empreinte TLS Chrome : d'où curl_cffi. Pagination `?page=N`, 10 offres/page.
    """
    s = curl_requests.Session(impersonate="chrome", timeout=40)
    s.headers.update({"Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"})

    cards = []
    for page in range(1, BNP_MAX_PAGES + 1):
        url = BNP_LIST_URL if page == 1 else f"{BNP_LIST_URL}?page={page}"
        r = s.get(url)
        if r.status_code != 200:
            raise RuntimeError(f"liste VIE page {page} → HTTP {r.status_code}")
        soup = BeautifulSoup(r.text, "html.parser")
        page_cards = soup.select("article.card-offer")
        if not page_cards:
            break
        cards.extend(page_cards)
        if not soup.select_one(f'div.pagination a[data-to="{page + 1}"]'):
            break
        time.sleep(1)
    return cards


def fmt_bnp_offer(card) -> "dict | None":
    link = card.select_one("a.card-link")
    title_el = card.select_one("h3")
    if not link or not title_el:
        return None
    href = link.get("href", "")
    slug = href.rstrip("/").rsplit("/", 1)[-1]
    offer_type = clean_text(card.select_one(".offer-type").get_text() if card.select_one(".offer-type") else "")
    title = clean_text(title_el.get_text())
    if offer_type and "VIE" not in offer_type.upper() and "V.I.E" not in title.upper():
        return None
    logo = card.select_one(".offer-logo img")
    entity = clean_text(logo.get("alt")) if logo else ""
    company = "BNP Paribas"
    if entity and entity.lower() != "bnp paribas":
        company += f" — {entity}"
    loc_el = card.select_one(".offer-location")
    return {
        "uid":       f"bnp_{slug}",
        "title":     title,
        "company":   company,
        "location":  clean_text(loc_el.get_text()) if loc_el else "",
        "published": None,   # la fiche n'expose que la date de mise à jour
        "url":       href if href.startswith("http") else BNP_BASE + href,
        "source":    "BNP Paribas",
        "color":     BNP_COLOR,
    }


def get_bnp_new(seen_ids: set) -> list:
    log("📡 BNP Paribas…")
    if not CURL_CFFI_AVAILABLE:
        log("   curl_cffi absent — source ignorée.")
        return []
    try:
        cards = retry(fetch_bnp_cards, "BNP")
        offers = unique_by_uid(o for o in (fmt_bnp_offer(c) for c in cards) if o)
        log(f"   {len(offers)} offre(s) VIE en ligne.")
        new = [o for o in offers if o["uid"] not in seen_ids]
        log(f"   {len(new)} nouvelle(s) offre(s).")
        return new
    except Exception as exc:
        log(f"[BNP erreur] {exc}")
        return []


# ── Source 4 : Natixis ─────────────────────────────────────────────────────────

def fetch_ntx_items() -> list:
    """API REST du portail BPCE : la même que celle qu'appelle la page React."""
    headers = {
        **JSON_HEADERS,
        "Origin": NTX_BASE,
        "Referer": NTX_BASE + "/",
    }
    items, offset = [], 0
    while True:
        body = {
            "lang": "fr", "keyword": "", "tax_sector": "", "tax_contract": "vie",
            "tax_place": "", "tax_experience": "", "tax_degree": "", "tax_brands": "",
            "tax_department": "", "tax_job": "", "tax_city": "", "tax_country": "",
            "tax_channel": "", "jobcode": "",
            "size": str(NTX_PAGE_SIZE), "from": str(offset),
        }
        r = requests.post(NTX_API_URL, json=body, headers=headers, timeout=40)
        r.raise_for_status()
        data = r.json().get("data") or {}
        page_items = data.get("items") or []
        items.extend(page_items)
        total = int(data.get("total") or 0)
        if not page_items or len(items) >= total:
            break
        offset += NTX_PAGE_SIZE
        time.sleep(1)
    return items


def fmt_ntx_offer(item: dict) -> dict:
    ref = clean_text(item.get("job_number") or item.get("post_id"))
    title = clean_text(item.get("title"))
    uid = f"ntx_{ref}" if ref else f"ntx_{hashlib.md5(title.encode()).hexdigest()[:12]}"

    locs = item.get("localisations") or []
    loc = locs[0] if locs and isinstance(locs[0], dict) else {}
    city = clean_text(loc.get("city") or loc.get("localisation") or item.get("localisation"))
    country = clean_text(loc.get("region"))
    if city and country and city != country:
        location = f"{city}, {country}"
    else:
        location = city or country

    brands = item.get("brand") or []
    brand = clean_text(brands[0]) if brands else ""
    company = "Natixis"
    if brand and brand.lower() != "natixis":
        company += f" — {brand}"

    link = item.get("link") or {}
    path = link.get("url") if isinstance(link, dict) else ""
    url = path if str(path).startswith("http") else NTX_BASE + str(path or "")

    return {
        "uid":       uid,
        "title":     title or "Sans titre",
        "company":   company,
        "location":  location,
        "published": parse_date(item.get("date")),
        "url":       url if path else NTX_SEARCH_PAGE,
        "source":    "Natixis",
        "color":     NTX_COLOR,
    }


def get_ntx_new(seen_ids: set) -> list:
    log("📡 Natixis…")
    try:
        items = retry(fetch_ntx_items, "Natixis")
        log(f"   {len(items)} offre(s) VIE en ligne.")
        offers = unique_by_uid(fmt_ntx_offer(i) for i in items if isinstance(i, dict))
        cutoff = lookback_cutoff()
        recent = [o for o in offers if o["published"] is None or o["published"] >= cutoff]
        new = [o for o in recent if o["uid"] not in seen_ids]
        log(f"   {len(new)} nouvelle(s) offre(s).")
        return new
    except Exception as exc:
        log(f"[Natixis erreur] {exc}")
        return []


# ── Doublons entre sources ─────────────────────────────────────────────────────

# Noms d'organisation Business France pouvant correspondre à chaque banque.
BANK_NAME_HINTS = {
    "Société Générale": ("societe generale", "socgen", "sg "),
    "BNP Paribas":      ("bnp", "bgl", "arval", "cardif"),
    "Natixis":          ("natixis", "bpce", "aew", "ostrum", "mirova"),
}


def _ascii_lower(s: str) -> str:
    s = unicodedata.normalize("NFKD", html.unescape(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def normalize_title(title: str) -> str:
    t = _ascii_lower(title)
    t = re.sub(r"\bv\.?\s?i\.?\s?e\.?\b", " ", t)                     # V.I.E, VIE
    t = re.sub(r"\b[hfm]\s*/\s*[hfm]\b", " ", t)                       # H/F, F/M…
    t = re.sub(r"\b\d+\s*(mois|months?|moths|motnhs)\b", " ", t)       # 12 mois, 18 months
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return " ".join(t.split())


def normalize_city(location: str) -> str:
    return _ascii_lower(location).split(",")[0].strip()


def is_same_bank(source: str, bf_company: str) -> bool:
    name = _ascii_lower(bf_company)
    return any(h in name for h in BANK_NAME_HINTS.get(source, ()))


def dedup_against_bf(bank_offers: list, bf_offers: list):
    """
    Écarte les offres banque déjà présentes chez Business France (même entreprise,
    titre très proche, et ville identique quand les deux sont connues).
    Retourne (offres conservées, offres écartées).
    """
    kept, dropped = [], []
    for offer in bank_offers:
        nt, city = normalize_title(offer["title"]), normalize_city(offer.get("location", ""))
        duplicate = None
        for bf in bf_offers:
            if not is_same_bank(offer["source"], bf.get("company", "")):
                continue
            ratio = SequenceMatcher(None, nt, normalize_title(bf["title"])).ratio()
            bf_city = normalize_city(bf.get("location", ""))
            same_city = bool(city and bf_city and city == bf_city)
            if ratio >= 0.9 or (ratio >= 0.75 and same_city):
                duplicate = bf
                break
        if duplicate:
            dropped.append(offer)
            log(f"   ↩ doublon Business France écarté : « {offer['title'][:60]} »")
        else:
            kept.append(offer)
    return kept, dropped


# ── Email ──────────────────────────────────────────────────────────────────────

def _offer_row(offer: dict) -> str:
    color    = offer.get("color", BF_COLOR)
    company  = html.escape(offer.get("company", ""))
    title    = html.escape(offer.get("title", ""))
    url      = html.escape(offer.get("url", ""), quote=True)

    parts = []
    if offer.get("location"):  parts.append(f"📍 {html.escape(offer['location'])}")
    if offer.get("duration"):  parts.append(f"⏱ {html.escape(offer['duration'])}")
    if offer.get("start"):     parts.append(f"🗓 {html.escape(offer['start'])}")
    if offer.get("indemnite"): parts.append(f"💶 {html.escape(offer['indemnite'])}")
    if offer.get("detail"):    parts.append(f"🏷 {html.escape(offer['detail'])}")
    if offer.get("published") and offer.get("source") != "Business France":
        parts.append(f"📅 publiée le {fmt_date(offer['published'])}")
    meta = " &nbsp;·&nbsp; ".join(parts)

    return f"""
          <tr>
            <td style="padding:14px 0;border-bottom:1px solid #eee">
              <a href="{url}"
                 style="display:block;font-size:15px;font-weight:700;color:#1a3c6e;
                        text-decoration:none">{title}</a>
              <span style="font-size:14px;color:#333;font-weight:600">{company}</span>
              {"<div style='margin-top:6px;font-size:13px;color:#666'>" + meta + "</div>" if meta else ""}
              <div style="margin-top:8px">
                <a href="{url}"
                   style="background:{color};color:#fff;padding:6px 16px;
                          border-radius:4px;font-size:12px;text-decoration:none;
                          font-weight:600">Voir l'offre →</a>
              </div>
            </td>
          </tr>"""


def _section_html(name: str, color: str, icon: str, offers: list) -> str:
    rows = "".join(_offer_row(o) for o in offers)
    return f"""
      <tr><td style="padding:22px 30px 0">
        <div style="border-left:4px solid {color};padding:6px 12px;background:#f8f9fb;
                    font-size:14px;font-weight:700;color:#1a3c6e">
          {icon} {name}
          <span style="color:{color};font-weight:600"> &nbsp;·&nbsp; {len(offers)} nouvelle(s)</span>
        </div>
        <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
      </td></tr>"""


def build_html(new_by_source: dict) -> str:
    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
    total   = sum(len(v) for v in new_by_source.values())

    summary_parts, sections, footer_links = [], [], []
    for name, color, icon, search_url in SOURCES:
        offers = new_by_source.get(name, [])
        footer_links.append(
            f'<a href="{search_url}" style="color:{color};font-size:12px;font-weight:600;'
            f'text-decoration:none">{name}</a>'
        )
        if offers:
            summary_parts.append(f"{icon} {len(offers)} {name}")
            sections.append(_section_html(name, color, icon, offers))
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
      <!-- Sections par source -->
      {"".join(sections)}
      <tr><td style="height:20px"></td></tr>
      <!-- Footer -->
      <tr><td style="background:#f4f6f9;padding:14px 30px;text-align:center;
                     border-radius:0 0 8px 8px">
        <p style="margin:0;font-size:11px;color:#999">Voir toutes les offres :</p>
        <p style="margin:6px 0 0">{" &nbsp;·&nbsp; ".join(footer_links)}</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if DRY_RUN:
        log("🧪 Mode DRY_RUN : aucun email, aucune écriture de l'historique.")
    else:
        validate_env()

    seen_ids = load_seen()
    to_save  = set(seen_ids)
    new_by_source = {name: [] for name, *_ in SOURCES}

    # ── Business France (à chaque passage)
    bf_new, bf_all = get_bf_new(seen_ids)
    new_by_source["Business France"] = bf_new
    to_save.update(o["uid"] for o in bf_new)

    # ── Banques (une fois par heure)
    if banks_due():
        for name, fetcher in (
            ("Société Générale", get_sg_new),
            ("BNP Paribas",      get_bnp_new),
            ("Natixis",          get_ntx_new),
        ):
            offers = fetcher(seen_ids)
            kept, dropped = dedup_against_bf(offers, bf_all)
            new_by_source[name] = kept
            # Les doublons sont aussi mémorisés pour ne pas réapparaître.
            to_save.update(o["uid"] for o in offers)
    else:
        log("⏭ Banques non interrogées sur ce passage (une fois par heure, "
            "FORCE_BANKS=1 pour forcer).")

    # ── Bilan
    total = sum(len(v) for v in new_by_source.values())
    log(f"\n📊 Total nouvelles offres : {total}")
    for name in new_by_source:
        log(f"   {name} : {len(new_by_source[name])}")

    if not total:
        log("Aucune nouvelle offre. Pas d'email envoyé.")
        if not DRY_RUN:
            save_seen(to_save)
        return

    html_body = build_html(new_by_source)
    summary = " · ".join(
        f"{name} {len(offers)}" for name, offers in new_by_source.items() if offers
    )
    subject = (
        f"🆕 {total} nouvelle(s) offre(s) VIE — {summary}"
        f" — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )

    if DRY_RUN:
        with open(PREVIEW_PATH, "w", encoding="utf-8") as f:
            f.write(html_body)
        log(f"🧪 Sujet : {subject}")
        log(f"🧪 Aperçu HTML écrit dans {PREVIEW_PATH}")
        return

    send_email(subject, html_body)
    save_seen(to_save)
    log("💾 Historique mis à jour.")


if __name__ == "__main__":
    main()
