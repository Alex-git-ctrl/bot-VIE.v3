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
LOOKBACK_DAYS = 3
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_offers.json")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "").strip()
RECIPIENT     = os.environ.get("RECIPIENT", GMAIL_ADDRESS).strip()

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
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
            "⚠️ Vérifie GMAIL_PASSWORD : un mot de passe d'application Gmail "
            "fait normalement 16 caractères.",
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

def fetch_bf_all():
    all_offers, skip = [], 0
    while True:
        try:
            resp = requests.post(
                BF_API_URL,
                json={**BF_PAYLOAD, "skip": skip},
                timeout=30,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[BF erreur skip={skip}] {exc}", file=sys.stderr)
            break
        data    = resp.json()
        total   = data.get("count", 0)
        results = data.get("result", [])
        seen    = {str(o.get("id")) for o in all_offers if o.get("id") is not None}
        for o in results:
            if o.get("id") is not None and str(o["id"]) not in seen:
                all_offers.append(o)
        if len(all_offers) >= total or len(results) < BF_PAYLOAD["limit"]:
            break
        skip += BF_PAYLOAD["limit"]
    return all_offers


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
    ind_str = f"{ind:,.0f} €/mois".replace(",", "\u202f") if ind else "Non précisée"
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
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=LOOKBACK_DAYS - 1)
        recent = [
            o for o in all_offers
            if (_parse_bf_date(o.get("startBroadcastDate")) or date.min) >= cutoff
        ]
        new = [o for o in recent if str(o.get("id")) not in seen_ids]
        print(f"   {len(new)} nouvelle(s) offre(s).", file=sys.stderr)
        return [fmt_bf_offer(o) for o in new]
    except Exception as exc:
        print(f"[BF erreur] {exc}", file=sys.stderr)
        return []


# ── Source 2 : BNP Paribas ─────────────────────────────────────────────────────

def get_bnp_new(seen_ids: set) -> list:
    """
    Scrape les offres VIE BNP via Playwright + playwright-stealth.
    BNP utilise Akamai Bot Manager : playwright-stealth masque navigator.webdriver
    et tous les indicateurs de navigation automatisée.
    Sélecteur : article[class*='card-offer']   /   Pagination : ?page=N
    """
    print("📡 BNP Paribas...", file=sys.stderr)
    if not PLAYWRIGHT_AVAILABLE:
        print("   Playwright absent — BNP Paribas ignorée.", file=sys.stderr)
        return []

    new_offers = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=HTTP_HEADERS["User-Agent"],
                extra_http_headers={"Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8"},
            )
            page_num = 1

            while True:
                url = BNP_BASE_URL if page_num == 1 else f"{BNP_BASE_URL}?page={page_num}"
                print(f"   Chargement BNP page {page_num} : {url}", file=sys.stderr)

                try:
                    pw_page = ctx.new_page()

                    # Anti-détection Akamai
                    if STEALTH_AVAILABLE:
                        stealth_sync(pw_page)
                    else:
                        pw_page.add_init_script(
                            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                            "window.chrome={runtime:{}};"
                        )

                    pw_page.goto(url, wait_until="domcontentloaded", timeout=45_000)

                    # Accepter la bannière Tarteaucitron si présente
                    for ck_sel in [
                        "#tarteaucitronPersonalize2",
                        "button:has-text('Tout accepter')",
                        "button:has-text('Accepter tout')",
                        "button:has-text('Accepter')",
                        "button:has-text('Accept')",
                    ]:
                        try:
                            btn = pw_page.wait_for_selector(ck_sel, timeout=3_000)
                            if btn:
                                btn.click()
                                print("   BNP cookies acceptés.", file=sys.stderr)
                                pw_page.wait_for_timeout(1_500)
                                break
                        except Exception:
                            continue

                    # Attendre explicitement les cartes d'offres
                    try:
                        pw_page.wait_for_selector(
                            "article[class*='card-offer']", timeout=12_000
                        )
                    except Exception:
                        pass

                    html = pw_page.content()
                    pw_page.close()

                except Exception as exc:
                    print(f"[BNP erreur page={page_num}] {exc}", file=sys.stderr)
                    break

                soup  = BeautifulSoup(html, "html.parser")
                cards = soup.find_all("article", class_=lambda c: c and "card-offer" in c)

                if not cards:
                    excerpt = BeautifulSoup(html, "html.parser").get_text()[:300].strip()
                    print(
                        f"[BNP] Aucune offre page {page_num}. Début de page :\n{excerpt}",
                        file=sys.stderr,
                    )
                    break

                print(f"   {len(cards)} offres trouvées page {page_num}.", file=sys.stderr)
                for card in cards:
                    link_tag = card.find("a", href=True)
                    if not link_tag:
                        continue
                    href = link_tag["href"]
                    if not href.startswith("http"):
                        href = "https://group.bnpparibas" + href

                    lines = [l.strip() for l in card.get_text(separator="\n").split("\n") if l.strip()]
                    title    = lines[1] if len(lines) > 1 else (lines[0] if lines else "Sans titre")
                    location = lines[-1].title() if len(lines) > 2 else ""

                    slug = href.rstrip("/").split("/")[-1]
                    uid  = f"bnp_{slug}"

                    if uid not in seen_ids:
                        new_offers.append({
                            "uid":      uid,
                            "title":    title,
                            "company":  "BNP Paribas",
                            "location": location,
                            "url":      href,
                            "source":   "BNP Paribas",
                            "color":    BNP_COLOR,
                        })

                next_link = soup.find("a", attrs={"data-to": str(page_num + 1)})
                if not next_link:
                    break
                page_num += 1

            browser.close()

    except Exception as exc:
        print(f"[BNP erreur général] {exc}", file=sys.stderr)

    print(f"   {len(new_offers)} nouvelle(s) offre(s) BNP.", file=sys.stderr)
    return new_offers


# ── Source 3 : Société Générale ────────────────────────────────────────────────

def get_sg_new(seen_ids: set) -> list:
    """
    Récupère les offres VIE Société Générale via l'API Algolia.
    Stratégie :
      1. Charger la page SocGen avec Playwright pour intercepter l'appel Algolia
         (App ID, API key, nom d'index sont dans les headers HTTP).
      2. Dès que les credentials sont capturés, appeler l'API Algolia directement
         avec le filtre jobType:COOPERATIVE → JSON propre, pas de scraping DOM.
    """
    print("📡 Société Générale...", file=sys.stderr)
    if not PLAYWRIGHT_AVAILABLE:
        print("   Playwright absent — source ignorée.", file=sys.stderr)
        return []

    new_offers = []
    algolia_info: dict = {}

    # ── Étape 1 : intercepter les requêtes Algolia ────────────────────────────
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx  = browser.new_context(user_agent=HTTP_HEADERS["User-Agent"])
            page = ctx.new_page()

            if STEALTH_AVAILABLE:
                stealth_sync(page)

            def _on_request(req):
                """Capte les credentials Algolia depuis les headers des requêtes."""
                if "algolia" not in req.url:
                    return
                hdrs = req.headers
                app_id = hdrs.get("x-algolia-application-id", "")
                api_key = hdrs.get("x-algolia-api-key", "")
                if not app_id:
                    return
                algolia_info["appId"]  = app_id
                algolia_info["apiKey"] = api_key
                m = re.search(r"/indexes/([^/?]+)", req.url)
                if m:
                    algolia_info["indexName"] = urllib.parse.unquote(m.group(1))
                # Capture aussi le corps pour connaître les noms de champs
                try:
                    body = req.post_data
                    if body:
                        algolia_info["sampleBody"] = json.loads(body)
                except Exception:
                    pass
                print(
                    f"   Algolia intercepté : appId={app_id}, "
                    f"index={algolia_info.get('indexName', '?')}",
                    file=sys.stderr,
                )

            page.on("request", _on_request)

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

            # Recharger la page de recherche après cookies pour que
            # les résultats Algolia se déclenchent
            if cookie_accepted or not algolia_info.get("appId"):
                print("   Rechargement pour déclencher Algolia...", file=sys.stderr)
                page.goto(SG_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(8_000)

            browser.close()

    except Exception as exc:
        print(f"[SG erreur Playwright] {exc}", file=sys.stderr)

    # ── Étape 2 : appel direct à l'API Algolia ────────────────────────────────
    if not algolia_info.get("appId") or not algolia_info.get("indexName"):
        print(
            "[SG] Impossible de récupérer les credentials Algolia — source ignorée.",
            file=sys.stderr,
        )
        print("   0 nouvelle(s) offre(s) SG.", file=sys.stderr)
        return []

    app_id     = algolia_info["appId"]
    api_key    = algolia_info["apiKey"]
    index_name = algolia_info["indexName"]

    try:
        url = (
            f"https://{app_id}-dsn.algolia.net"
            f"/1/indexes/{urllib.parse.quote(index_name, safe='')}/query"
        )
        headers = {
            "X-Algolia-Application-Id": app_id,
            "X-Algolia-API-Key":        api_key,
            "Content-Type":             "application/json",
        }
        # Essaie d'abord avec facetFilters (format standard Algolia InstantSearch)
        body = {
            "facetFilters": [["jobType:COOPERATIVE"]],
            "hitsPerPage":  200,
            "page":         0,
        }
        resp = requests.post(url, json=body, headers=headers, timeout=30)
        if resp.status_code != 200:
            # Fallback : filters string
            body["filters"] = "jobType:COOPERATIVE"
            del body["facetFilters"]
            resp = requests.post(url, json=body, headers=headers, timeout=30)
        resp.raise_for_status()

        hits = resp.json().get("hits", [])
        print(f"   {len(hits)} offres COOPERATIVE via API Algolia.", file=sys.stderr)

        for hit in hits:
            title = (
                hit.get("title")
                or hit.get("jobTitle")
                or hit.get("name")
                or hit.get("label")
                or ""
            ).strip()
            if not title:
                continue

            href = (hit.get("url") or hit.get("link") or hit.get("applyUrl") or "").strip()
            if href and not href.startswith("http"):
                href = SG_BASE + href
            if not href:
                obj_id = hit.get("objectID", "")
                if obj_id:
                    href = f"{SG_BASE}/en/offer/{obj_id}"

            location = (
                hit.get("location")
                or hit.get("city")
                or hit.get("country")
                or hit.get("place")
                or ""
            ).strip()

            uid = f"sg_{hit.get('objectID', hashlib.md5(title.encode()).hexdigest()[:12])}"

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

    except Exception as exc:
        print(f"[SG erreur API Algolia] {exc}", file=sys.stderr)

    print(f"   {len(new_offers)} nouvelle(s) offre(s) SG.", file=sys.stderr)
    return new_offers


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

    counts      = {"Business France": 0, "BNP Paribas": 0, "Société Générale": 0}
    all_new     = []
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
