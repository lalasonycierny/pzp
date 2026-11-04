#!/usr/bin/env python3
"""
Denná kontrola platnosti PZP pre zoznam vozidiel.

- Pre každé EČV zistí, či je PZP platné v deň spustenia.
- Vytvorí tabuľku (report.html + GitHub Job Summary):
    zelená  = PZP platné
    červená = PZP NEPLATNÉ
    oranžová = nepodarilo sa overiť (chyba / neznáma odpoveď)
- Ak je aspoň jedno vozidlo bez platného PZP (alebo sa nedá overiť),
  odošle e-mail na nastavených príjemcov.

Nastavenie cez premenné prostredia (v GitHub Actions: Secrets / Variables):
    PZP_URL          URL endpointu overenia (pozri návod nižšie)
    PZP_METHOD       GET alebo POST (predvolené GET)
    PZP_ECV_FIELD    názov parametra pre EČV (predvolené "ecv")
    PZP_DATE_FIELD   názov parametra pre dátum (predvolené "date")
    PZP_BODY_JSON    1 = poslať POST ako JSON, inak ako form-data (predvolené 0)
    SMTP_HOST        predvolené smtp.gmail.com
    SMTP_PORT        predvolené 465 (SSL)
    SMTP_USER        e-mailová adresa odosielateľa
    SMTP_PASSWORD    heslo aplikácie (Gmail App Password)

Spustenie lokálne na test bez internetu a bez e-mailu:
    python check_pzp.py --demo
"""

import argparse
import html
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------------------
# Konfigurácia
# ----------------------------------------------------------------------------

TZ = ZoneInfo("Europe/Bratislava")

RECIPIENTS = ["woletzd@gmail.com", "dusan.woletz@orange.com"]

# (EČV, značka/model, farba)
VEHICLES = [
    ("BA037GU", "RENAULT CLIO THALIA", "Modrá"),
    ("WWWWWWW", "OPEL AGILA 1.2/16V", "Zelená metalíza svetlá"),
    ("BA156AY", "HONDA SCV100", "Čierna metalíza"),
    ("III11II", "LAND ROVER Freelander 2", "Čierna metalíza"),
    ("BA952AS", "PIAGGIO ZIP 50 2T", "Biela"),
    ("BA094AS", "TOMOS FLEXER", "Šedá metalíza"),
    ("BL043YM", "AGADOS NP 26 (prívesný vozík)", "Šedá"),
    ("BT732FP", "HONDA NSC110MPD", "Šedá"),
    ("IIIII1I", "BMW R 1250 GS Adventure", "Modrá metalíza"),
    ("II1IIII", "HONDA C125A", "Šedá"),
    ("II1III1", "HONDA Z125MA", "Žltá"),
    ("BT651DZ", "HONDA ADV750", "Žltá"),
]

VALID = "valid"
INVALID = "invalid"
UNKNOWN = "unknown"

LABELS = {
    VALID: "PZP platné",
    INVALID: "PZP NEPLATNÉ",
    UNKNOWN: "Nepodarilo sa overiť",
}
COLORS = {VALID: "#c6efce", INVALID: "#ffc7ce", UNKNOWN: "#ffeb9c"}
EMOJI = {VALID: "🟢", INVALID: "🔴", UNKNOWN: "🟠"}


# ----------------------------------------------------------------------------
# Overenie PZP
# ----------------------------------------------------------------------------

def is_insured(payload):
    """
    Z odpovede servera urči, či je vozidlo poistené.
    Vracia True / False / None (None = neviem určiť).

    Heuristika: ak odpoveď obsahuje aspoň jednu poisťovňu, PZP je platné;
    ak je zoznam poisťovní prázdny, PZP platné nie je.
    !!! Ak má skutočná odpoveď SKP inú štruktúru, upravte iba túto funkciu. !!!
    """
    if isinstance(payload, list):
        return len(payload) > 0

    if isinstance(payload, dict):
        for key, value in payload.items():
            k = key.lower()
            if any(part in k for part in ("poist", "insur")):
                if isinstance(value, (list, dict, str)):
                    return len(value) > 0
                if isinstance(value, bool):
                    return value
        for key in ("valid", "platne", "isValid", "insured"):
            if isinstance(payload.get(key), bool):
                return payload[key]
    return None


def check_pzp(session, ecv, day):
    """Vráti (stav, detail) pre dané EČV a dátum `day` (YYYY-MM-DD)."""
    url = os.environ.get("PZP_URL", "").strip()
    if not url:
        return UNKNOWN, "Nie je nastavená PZP_URL"

    method = os.environ.get("PZP_METHOD", "GET").upper()
    ecv_field = os.environ.get("PZP_ECV_FIELD", "ecv")
    date_field = os.environ.get("PZP_DATE_FIELD", "date")
    as_json = os.environ.get("PZP_BODY_JSON", "0") == "1"
    data = {ecv_field: ecv, date_field: day}

    last_error = ""
    for attempt in range(3):
        try:
            if method == "POST":
                if as_json:
                    resp = session.post(url, json=data, timeout=30)
                else:
                    resp = session.post(url, data=data, timeout=30)
            else:
                resp = session.get(url, params=data, timeout=30)
            resp.raise_for_status()
            result = is_insured(resp.json())
            if result is True:
                return VALID, ""
            if result is False:
                return INVALID, ""
            return UNKNOWN, "Neznáma štruktúra odpovede"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2 * (attempt + 1))
    return UNKNOWN, last_error


# ----------------------------------------------------------------------------
# Report (HTML / Markdown)
# ----------------------------------------------------------------------------

def build_html_table(results):
    rows = []
    for r in results:
        color = COLORS[r["status"]]
        detail = f" ({html.escape(r['detail'])})" if r["detail"] else ""
        rows.append(
            f'<tr style="background:{color}">'
            f'<td style="padding:6px 10px;border:1px solid #999"><b>{html.escape(r["ecv"])}</b></td>'
            f'<td style="padding:6px 10px;border:1px solid #999">{html.escape(r["model"])}</td>'
            f'<td style="padding:6px 10px;border:1px solid #999">{html.escape(r["color"])}</td>'
            f'<td style="padding:6px 10px;border:1px solid #999">{LABELS[r["status"]]}{detail}</td>'
            "</tr>"
        )
    header = (
        "<tr style=\"background:#ddd\">"
        '<th style="padding:6px 10px;border:1px solid #999;text-align:left">EČV</th>'
        '<th style="padding:6px 10px;border:1px solid #999;text-align:left">Vozidlo</th>'
        '<th style="padding:6px 10px;border:1px solid #999;text-align:left">Farba</th>'
        '<th style="padding:6px 10px;border:1px solid #999;text-align:left">Stav PZP</th>'
        "</tr>"
    )
    return (
        '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px">'
        f"{header}{''.join(rows)}</table>"
    )


def build_html_page(results, day):
    return (
        '<!DOCTYPE html><html lang="sk"><head><meta charset="utf-8">'
        f"<title>Kontrola PZP {day}</title></head>"
        '<body style="font-family:Arial,sans-serif;padding:16px">'
        f"<h2>Kontrola PZP k {day}</h2>{build_html_table(results)}</body></html>"
    )


def build_markdown(results, day):
    lines = [
        f"## Kontrola PZP k {day}",
        "",
        "| | EČV | Vozidlo | Farba | Stav PZP |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        detail = f" ({r['detail']})" if r["detail"] else ""
        lines.append(
            f"| {EMOJI[r['status']]} | **{r['ecv']}** | {r['model']} | {r['color']} | "
            f"{LABELS[r['status']]}{detail} |"
        )
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# E-mail
# ----------------------------------------------------------------------------

def send_email(results, day):
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    if not user or not password:
        raise RuntimeError("Chýba SMTP_USER alebo SMTP_PASSWORD")

    bad = [r for r in results if r["status"] == INVALID]
    unknown = [r for r in results if r["status"] == UNKNOWN]

    parts = []
    if bad:
        parts.append(f"{len(bad)}x PZP NEPLATNÉ")
    if unknown:
        parts.append(f"{len(unknown)}x nepodarilo sa overiť")
    subject = f"PZP {day}: " + ", ".join(parts)

    text_lines = [f"Kontrola PZP k {day}", ""]
    for r in results:
        detail = f" ({r['detail']})" if r["detail"] else ""
        text_lines.append(f"{r['ecv']} - {r['model']}: {LABELS[r['status']]}{detail}")
    text = "\n".join(text_lines)

    intro = ""
    if bad:
        intro += (
            "<p><b>PZP nie je platné pre:</b> "
            + ", ".join(html.escape(r["ecv"]) for r in bad)
            + "</p>"
        )
    if unknown:
        intro += (
            "<p><b>Nepodarilo sa overiť:</b> "
            + ", ".join(html.escape(r["ecv"]) for r in unknown)
            + "</p>"
        )
    body_html = (
        f'<html><body style="font-family:Arial,sans-serif">'
        f"<h3>Kontrola PZP k {day}</h3>{intro}{build_html_table(results)}</body></html>"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(RECIPIENTS)
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    with smtplib.SMTP_SSL(host, port, timeout=60) as server:
        server.login(user, password)
        server.sendmail(user, RECIPIENTS, msg.as_string())
    print(f"E-mail odoslaný na: {', '.join(RECIPIENTS)}")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Nevolá žiadny server ani neposiela e-mail, iba vygeneruje ukážkový report.",
    )
    args = parser.parse_args()

    day = datetime.now(TZ).strftime("%Y-%m-%d")
    print(f"Kontrola PZP k dátumu {day}")

    results = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (pzp-check; GitHub Actions)"})

    for i, (ecv, model, color) in enumerate(VEHICLES):
        ecv_norm = ecv.replace(" ", "").upper()
        if args.demo:
            status = [VALID, VALID, INVALID, UNKNOWN][i % 4]
            detail = ""
        else:
            status, detail = check_pzp(session, ecv_norm, day)
            time.sleep(1)  # slušné tempo voči serveru
        results.append(
            {"ecv": ecv_norm, "model": model, "color": color, "status": status, "detail": detail}
        )
        print(f"{EMOJI[status]} {ecv_norm:8} {model}: {LABELS[status]} {detail}")

    # Report
    with open("report.html", "w", encoding="utf-8") as f:
        f.write(build_html_page(results, day))
    md = build_markdown(results, day)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(md)
    else:
        print("\n" + md)

    problems = [r for r in results if r["status"] != VALID]
    if not problems:
        print("Všetky vozidlá majú platné PZP.")
        return 0

    if args.demo:
        print("Demo režim: e-mail sa neposiela.")
        return 1

    try:
        send_email(results, day)
    except Exception as exc:  # noqa: BLE001
        print(f"CHYBA pri odosielaní e-mailu: {exc}", file=sys.stderr)
        return 2
    return 1  # job v GitHube sa označí ako červený


if __name__ == "__main__":
    sys.exit(main())
