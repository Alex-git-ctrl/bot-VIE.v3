#!/usr/bin/env python3
"""
Bot de veille VIE - Business France

Récupère les offres VIE récentes et envoie un email uniquement lorsqu'une
ou plusieurs nouvelles offres apparaissent.

Variables d'environnement requises (GitHub Secrets) :
  GMAIL_ADDRESS  -> adresse Gmail expéditrice
  GMAIL_PASSWORD -> mot de passe d'application Gmail (16 caractères)
  RECIPIENT      -> adresse de réception (facultatif, défaut = GMAIL_ADDRESS)
"""

import json
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

API_URL = "https://civiweb-api-prd.azurewebsites.net/api/Offers/search"
OFFER_BASE = "https://mon-vie-via.businessfrance.fr/offres"
SEARCH_URL = (
    "https://mon-vie-via.businessfrance.fr/offres/recherche"
    "?query&specializationsIds=19&geographicZones=2"
    "&geographicZones=3&geographicZones=4&teletravail=0&porteEnv=0"
)

BASE_PAYLOAD = {
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

LOOKBACK_DAYS = 3
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_offers.json")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "").strip()
RECIPIENT = os.environ.get("RECIPIENT", GMAIL_ADDRESS).strip()


def validate_env():
    if not GMAIL_ADDRESS:
        raise RuntimeError("Le secret GMAIL_ADDRESS est absent ou vide.")
    if not GMAIL_PASSWORD:
        raise RuntimeError("Le secret GMAIL_PASSWORD est absent ou vide.")
    if not RECIPIENT:
        raise RuntimeError("Le secret RECIPIENT est absent ou vide.")
    if len(GMAIL_PASSWORD) != 16:
        print("⚠️ Vérifie GMAIL_PASSWORD : un mot de passe d'application Gmail fait normalement 16 caractères.", file=sys.stderr)


def fetch_all_offers():
    all_offers = []
    skip = 0

    while True:
        try:
            response = requests.post(
                API_URL,
                json={**BASE_PAYLOAD, "skip": skip},
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"[ERREUR API skip={skip}] {exc}", file=sys.stderr)
            break

        data = response.json()
        total = data.get("count", 0)
        results = data.get("result", [])

        existing_ids = {str(offer.get("id")) for offer in all_offers if offer.get("id") is not None}
        for offer in results:
            oid = offer.get("id")
            if oid is not None and str(oid) not in existing_ids:
                all_offers.append(offer)

        if len(all_offers) >= total or len(results) < BASE_PAYLOAD["limit"]:
            break

        skip += BASE_PAYLOAD["limit"]

    return all_offers


def parse_date(raw):
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "")).date()
    except ValueError:
        return None


def filter_recent(offers):
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=LOOKBACK_DAYS - 1)
    return [
        offer for offer in offers
        if (parse_date(offer.get("startBroadcastDate")) or date.min) >= cutoff
    ]


def sort_by_broadcast(offers):
    def key(offer):
        raw = offer.get("startBroadcastDate", "")
        try:
            return datetime.fromisoformat(raw.replace("Z", "")) if raw else datetime.min
        except ValueError:
            return datetime.min

    return sorted(offers, key=key)


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()

    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {str(x) for x in data.get("seen_ids", [])}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"⚠️ Impossible de lire {SEEN_FILE}: {exc}", file=sys.stderr)
        return set()


def save_seen(ids):
    payload = {
        "seen_ids": sorted(str(x) for x in ids),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def fmt_offer(offer):
    oid = offer.get("id")
    start = offer.get("missionStartDate", "")

    try:
        start_str = (
            datetime.fromisoformat(start.replace("Z", "")).strftime("%B %Y")
            if start else "Non précisé"
        )
    except ValueError:
        start_str = start[:7] if start else "Non précisé"

    ind = offer.get("indemnite")
    ind_str = f"{ind:,.0f} €/mois".replace(",", "\u202f") if ind else "Non précisée"

    return {
        "id": str(oid) if oid is not None else "",
        "title": offer.get("missionTitle", "Sans titre"),
        "company": offer.get("organizationName", "?"),
        "location": f"{offer.get('cityName', '?')}, {offer.get('countryName', '?')}",
        "duration": f"{offer.get('missionDuration', '?')} mois",
        "start": start_str,
        "indemnite": ind_str,
        "url": f"{OFFER_BASE}/{oid}",
    }


def build_html(offers, total_recent):
    now_str = datetime.now().strftime("%d/%m/%Y à %H:%M")
    count = len(offers)
    rows = ""

    for offer in offers:
        rows += f"""
      <tr>
        <td style="padding:14px 0;border-bottom:1px solid #eee">
          <a href="{offer['url']}" style="font-size:15px;font-weight:700;color:#1a3c6e;text-decoration:none">{offer['title']}</a><br>
          <span style="font-size:14px;color:#333;font-weight:600">{offer['company']}</span>
          <div style="margin-top:6px;font-size:13px;color:#666">
            📍 {offer['location']} &nbsp;·&nbsp; ⏱ {offer['duration']} &nbsp;·&nbsp; 🗓 {offer['start']} &nbsp;·&nbsp; 💶 {offer['indemnite']}
          </div>
          <div style="margin-top:8px">
            <a href="{offer['url']}" style="background:#1a3c6e;color:#fff;padding:6px 16px;border-radius:4px;font-size:12px;text-decoration:none;font-weight:600">Voir l'offre →</a>
          </div>
        </td>
      </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f4f6f9;font-family:Arial,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:30px 0">
  <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0"
           style="background:#fff;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.08)">
      <tr><td style="background:#1a3c6e;padding:22px 30px;border-radius:8px 8px 0 0">
        <h2 style="margin:0;color:#fff;font-size:19px">🆕 Nouvelle(s) offre(s) VIE détectée(s)</h2>
        <p style="margin:5px 0 0;color:#aec6e8;font-size:13px">
          {count} nouvelle(s) offre(s) détectée(s) le {now_str} &nbsp;·&nbsp; {total_recent} offre(s) récente(s) surveillée(s)
        </p>
      </td></tr>
      <tr><td style="padding:8px 30px 20px">
        <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
      </td></tr>
      <tr><td style="background:#f4f6f9;padding:14px 30px;text-align:center;border-radius:0 0 8px 8px">
        <a href="{SEARCH_URL}" style="color:#1a3c6e;font-size:13px;font-weight:600;text-decoration:none">
          Voir toutes les offres sur Business France →
        </a>
        <p style="margin:5px 0 0;font-size:11px;color:#999">Finance · Amériques &amp; Asie/Pacifique</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


def send_email(subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, RECIPIENT, msg.as_string())
        print("✅ Email envoyé.", file=sys.stderr)

    except smtplib.SMTPAuthenticationError as exc:
        print("❌ Gmail a refusé l'authentification SMTP.", file=sys.stderr)
        print(f"   Code SMTP : {exc.smtp_code}", file=sys.stderr)
        print(f"   Réponse SMTP : {exc.smtp_error}", file=sys.stderr)
        raise


def main():
    validate_env()

    print("📡 Récupération des offres VIE...", file=sys.stderr)
    all_offers = fetch_all_offers()
    print(f"   {len(all_offers)} offres récupérées.", file=sys.stderr)

    recent_offers = sort_by_broadcast(filter_recent(all_offers))
    print(f"   {len(recent_offers)} offre(s) récente(s) sur les {LOOKBACK_DAYS} derniers jours.", file=sys.stderr)

    if not recent_offers:
        print("Aucune offre récente. Pas d'email envoyé.", file=sys.stderr)
        return

    seen_ids = load_seen()
    new_offers = [
        offer for offer in recent_offers
        if str(offer.get("id")) not in seen_ids
    ]

    print(f"   {len(new_offers)} nouvelle(s) offre(s) détectée(s).", file=sys.stderr)

    if not new_offers:
        print("Aucune nouvelle offre. Pas d'email envoyé.", file=sys.stderr)
        return

    formatted_new_offers = [fmt_offer(offer) for offer in new_offers]
    html = build_html(formatted_new_offers, len(recent_offers))
    subject = f"🆕 {len(new_offers)} nouvelle(s) offre(s) VIE — {datetime.now().strftime('%d/%m/%Y %H:%M')}"

    send_email(subject, html)

    updated_seen = set(seen_ids)
    updated_seen.update(
        str(offer.get("id"))
        for offer in recent_offers
        if offer.get("id") is not None
    )
    save_seen(updated_seen)
    print("💾 Historique mis à jour.", file=sys.stderr)


if __name__ == "__main__":
    main()