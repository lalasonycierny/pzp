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
        return VALID, insurer

    return UNKNOWN, "Neznáma štruktúra odpovede"


def _first_visible(candidates):
    """Z kandidátov (Playwright locators) vráti prvý, ktorý existuje a je viditeľný."""
    for loc in candidates:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:  # noqa: BLE001
            continue
    return None


def _dismiss_cookies(page):
    for name in ("Prijať všetko", "Povoliť všetko", "Súhlasím", "Prijať", "Rozumiem", "Accept all"):
        try:
            btn = page.get_by_role("button", name=name)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=2000)
                return
        except Exception:  # noqa: BLE001
            continue


def check_one(page, ecv, today):
    """Overí jedno EČV. Vráti (stav, detail)."""
    page.goto(PZP_PAGE_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:  # noqa: BLE001
        pass
    _dismiss_cookies(page)

    ecv_input = _first_visible([
        page.get_by_label("Evidenčné číslo vozidla", exact=False),
        page.get_by_placeholder(re.compile("EČV|Evidenčné", re.IGNORECASE)),
        page.locator("input[type='text']"),
    ])
    if ecv_input is None:
        return UNKNOWN, "Nenašlo sa pole pre EČV"
    ecv_input.fill(ecv)

    # Dátum: vyplní sa iba ak je pole prázdne (inak ostane predvolený dnešný)
    date_input = _first_visible([
        page.get_by_label("Poistenie ku dňu", exact=False),
        page.locator("input[type='date']"),
    ])
    if date_input is not None:
        try:
            if not date_input.input_value():
                if date_input.get_attribute("type") == "date":
                    date_input.fill(today.strftime("%Y-%m-%d"))
                else:
                    date_input.fill(today.strftime("%d.%m.%Y"))
        except Exception:  # noqa: BLE001
            pass

    button = _first_visible([
        page.get_by_role("button", name="Overiť poistenie"),
        page.get_by_text("Overiť poistenie", exact=False),
    ])
    if button is None:
        return UNKNOWN, "Nenašlo sa tlačidlo Overiť poistenie"
    button.click()

    # Výsledok je hotový, keď sa v zobrazenom texte objaví zadané EČV
    # (pole input sa do innerText nepočíta, skryté šablóny tiež nie).
    try:
        page.wait_for_function(
            "(ecv) => document.body.innerText.toUpperCase().includes(ecv)",
            arg=ecv,
            timeout=30000,
        )
    except Exception:  # noqa: BLE001
        return UNKNOWN, "Výsledok sa nezobrazil (captcha / chyba stránky?)"
    time.sleep(1.5)  # doznie načítanie (spinner)

    text = page.inner_text("body")
    return parse_result(text, today)


def run_checks(today):
    """Overí všetky vozidlá. Vráti zoznam výsledkov."""
    from playwright.sync_api import sync_playwright  # import až tu, aby fungoval --demo

    os.makedirs(DEBUG_DIR, exist_ok=True)
    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="sk-SK",
            timezone_id="Europe/Bratislava",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        page.set_default_timeout(30000)

        for ecv, model, color in VEHICLES:
            ecv_norm = ecv.replace(" ", "").upper()
            status, detail = UNKNOWN, ""
            for attempt in range(2):
                try:
                    status, detail = check_one(page, ecv_norm, today)
                except Exception as exc:  # noqa: BLE001
                    status, detail = UNKNOWN, f"{type(exc).__name__}: {str(exc)[:120]}"
                if status != UNKNOWN:
                    break
                time.sleep(3)
            if status == UNKNOWN:
                try:
                    page.screenshot(path=os.path.join(DEBUG_DIR, f"{ecv_norm}.png"), full_page=True)
                except Exception:  # noqa: BLE001
                    pass
            results.append(
                {"ecv": ecv_norm, "model": model, "color": color, "status": status, "detail": detail}
            )
            print(f"{EMOJI[status]} {ecv_norm:8} {model}: {LABELS[status]} {detail}")
            time.sleep(1)  # slušné tempo voči serveru

        browser.close()
    return results


# ----------------------------------------------------------------------------
# Report (HTML / Markdown)
# ----------------------------------------------------------------------------

def build_html_table(results):
    cell = 'style="padding:6px 10px;border:1px solid #999;text-align:left"'
    rows = []
    for r in results:
        detail = f" ({html.escape(r['detail'])})" if r["detail"] else ""
        rows.append(
            f'<tr style="background:{COLORS[r["status"]]}">'
            f'<td {cell}><b>{html.escape(r["ecv"])}</b></td>'
            f'<td {cell}>{html.escape(r["model"])}</td>'
            f'<td {cell}>{html.escape(r["color"])}</td>'
            f'<td {cell}>{LABELS[r["status"]]}{detail}</td>'
            "</tr>"
        )
    header = (
        '<tr style="background:#ddd">'
        f"<th {cell}>EČV</th><th {cell}>Vozidlo</th><th {cell}>Farba</th><th {cell}>Stav PZP</th>"
        "</tr>"
    )
    return (
        '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px">'
        f"{header}{''.join(rows)}</table>"
    )


def build_html_page(results, day, generated):
    bad = sum(1 for r in results if r["status"] == INVALID)
    unknown = sum(1 for r in results if r["status"] == UNKNOWN)
    if bad:
        banner_color, banner = "#c00000", f"{bad}x PZP NEPLATNÉ"
        if unknown:
            banner += f", {unknown}x nepodarilo sa overiť"
    elif unknown:
        banner_color, banner = "#b36b00", f"{unknown}x nepodarilo sa overiť"
    else:
        banner_color, banner = "#2e7d32", "Všetky vozidlá majú platné PZP"

    return (
        '<!DOCTYPE html><html lang="sk"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>Kontrola PZP {day}</title></head>"
        '<body style="font-family:Arial,sans-serif;padding:16px;background:#fff;color:#000">'
        f"<h2>Kontrola PZP k {day}</h2>"
        f'<p style="font-weight:bold;color:{banner_color}">{banner}</p>'
        f'<div style="overflow-x:auto">{build_html_table(results)}</div>'
        f'<p style="color:#555;font-size:12px">Aktualizované: {generated} '
        "(automatická denná kontrola, zdroj: SKP)</p>"
        "</body></html>"
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
    text_lines += ["", f"Prehľad: {PAGE_URL}"]
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
        '<html><body style="font-family:Arial,sans-serif">'
        f"<h3>Kontrola PZP k {day}</h3>{intro}{build_html_table(results)}"
        f'<p><a href="{PAGE_URL}">Aktuálny prehľad online</a></p></body></html>'
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
        help="Nevolá SKP ani neposiela e-mail, iba vygeneruje ukážkový report.",
    )
    args = parser.parse_args()

    now = datetime.now(TZ)
    today = now.date()
    day = today.strftime("%Y-%m-%d")
    generated = now.strftime("%Y-%m-%d %H:%M")
    print(f"Kontrola PZP k dátumu {day}")

    if args.demo:
        results = []
        for i, (ecv, model, color) in enumerate(VEHICLES):
            status = [VALID, VALID, INVALID, UNKNOWN][i % 4]
            results.append(
                {"ecv": ecv, "model": model, "color": color, "status": status, "detail": ""}
            )
    else:
        results = run_checks(today)

    # Report
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_html_page(results, day, generated))

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
