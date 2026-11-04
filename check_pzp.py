#!/usr/bin/env python3
"""
Denná kontrola platnosti PZP pre zoznam vozidiel (SKP).

- Pre každé EČV otvorí stránku SKP v headless prehliadači, vyplní EČV a dnešný
  dátum, klikne na "Overiť poistenie" a prečíta výsledok.
- Vytvorí tabuľku site/index.html (zelená = platné, červená = neplatné,
  oranžová = nepodarilo sa overiť) a zápis do GitHub Job Summary.
- Ak je aspoň jedno vozidlo bez platného PZP (alebo sa nedá overiť),
  odošle e-mail na nastavených príjemcov.

V GitHub Secrets sú potrebné iba:
    SMTP_USER        e-mailová adresa odosielateľa
    SMTP_PASSWORD    heslo aplikácie (Gmail App Password)
Voliteľne: SMTP_HOST (predvolené smtp.gmail.com), SMTP_PORT (predvolené 465).

Lokálny test bez internetu a bez e-mailu:
    python check_pzp.py --demo
"""

import argparse
import html
import os
import re
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Konfigurácia
# ----------------------------------------------------------------------------

TZ = ZoneInfo("Europe/Bratislava")

RECIPIENTS = ["woletzd@gmail.com", "dusan.woletz@orange.com"]

PZP_PAGE_URL = "https://www.skp.sk/vyhladat-poistovatela-vozidla-a-overit-platnost-pzp/"

# Adresa, na ktorej bude verejne dostupný index.html (GitHub Pages)
PAGE_URL = "https://lalasonycierny.github.io/pzp/"

OUTPUT_DIR = "site"      # sem sa zapíše index.html (publikuje sa na GitHub Pages)
DEBUG_DIR = "debug"      # screenshoty pri chybe overenia

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
# Overenie PZP na stránke SKP (Playwright)
# ----------------------------------------------------------------------------

def parse_result(text, today):
    """
    Z textu výsledku určí stav. Vracia (stav, detail).

    - "K zadaným údajom nebol v databáze SKP nájdený žiadny záznam" -> neplatné
    - zobrazená poisťovňa + číslo zmluvy -> platné, ak dnes spadá do
      obdobia "Poistenie od" / "Poistenie do" (prázdne "do" = na neurčito)
    """
    t = re.sub(r"\s+", " ", text)

    if re.search(r"žiadny záznam", t, re.IGNORECASE):
        return INVALID, "v databáze SKP nie je záznam"

    if "Číslo poistnej zmluvy" in t:
        insurer_m = re.search(r"Poisťovateľ (.+?) Číslo poistnej zmluvy", t)
        insurer = insurer_m.group(1).strip() if insurer_m else ""

        from_m = re.search(r"Poistenie od (\d{2}\.\d{2}\.\d{4})", t)
        to_m = re.search(r"Poistenie do (\d{2}\.\d{2}\.\d{4})", t)

        if from_m:
            start = datetime.strptime(from_m.group(1), "%d.%m.%Y").date()
            if start > today:
                return INVALID, f"{insurer}, poistenie začína až {from_m.group(1)}"
        if to_m:
            end = datetime.strptime(to_m.group(1), "%d.%m.%Y").date()
            if end < today:
                return INVALID, f"{insurer}, poistenie skončilo {to_m.group(1)}"
            return VALID, f"{insurer}, do {to_m.group(1)}"
        return VALID,
