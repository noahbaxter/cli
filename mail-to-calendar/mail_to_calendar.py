#!/usr/bin/env python3
"""Scan macOS Mail inboxes for events, extract with a local Ollama model, add to Calendar."""

import email
import email.header
import email.utils
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import questionary
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from rapidfuzz import fuzz
from rich.console import Console
from rich.table import Table

console = Console()

MAIL_BASE = Path.home() / "Library" / "Mail" / "V10"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:7b"
LOOKBACK_DAYS = 90

# UUID -> email address (sampled from To: headers)
ACCOUNT_MAP = {
    "25724148-63CB-444B-BD75-2E430F451B42": "lockslipband@gmail.com",
    "38518BD4-1EE6-45FF-BAC9-CD3AD415F1BD": "dotcomx23@gmail.com",
    "658549FC-4E2C-4B94-86BD-DAA8040FC762": "noahbaxt@gmail.com",
    "B7013E4E-23BE-4F04-B6D8-B397F8D87993": "noah.baxter@sonance.com",
}

# Pre-filter: must have at least one EVENT signal AND one DATE signal, with no NOISE bailout
EVENT_SIGNALS = [
    r'\b(meeting|conference|webinar|zoom|teams\s+call|invite|invitation)\b',
    r'\b(dinner|lunch|breakfast|happy\s+hour|drinks|coffee)\b',
    r'\b(show|concert|festival|gig|performance|premiere|screening|comedy|standup|tour)\b',
    r'\b(ticket|reservation|booking|rsvp|you.re\s+going|you.re\s+invited|doors\s+open)\b',
    r'\b(upcoming|join\s+us|save\s+the\s+date)\b',
]

DATE_SIGNALS = [
    r'\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
    r'\b\d{1,2}:\d{2}\s*(am|pm)\b',
    r'\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\b',
    r'\b\d{1,2}/\d{1,2}/\d{2,4}\b',
    r'\bthis\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
    r'\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b',
]

# Skip anything that looks primarily like transactional noise
NOISE_PATTERNS = [
    r'\b(your\s+order|has\s+shipped|has\s+been\s+delivered|order\s+confirmation|order\s+receipt)\b',
    r'\b(payment\s+received|payment\s+processed|auto\s+pay|bill\s+is\s+ready|invoice)\b',
    r'\b(sign.in|login|verification\s+code|account\s+registration|password\s+has\s+changed)\b',
    r'\b(improve\s+google|unsubscribe|click\s+here|view\s+in\s+browser)\b',
]

EXTRACTION_PROMPT = """\
Today is {today}. Extract any event or appointment referenced in this email.
Include ticket confirmations, show announcements, meetings, dinners, or any scheduled activity.
Resolve relative days ("this Thursday", "next Friday") from the email date: {email_date}.

Return a JSON object: {{"events": [...]}}
Each event item has:
  "title"    : event name (string)
  "date"     : YYYY-MM-DD (string, required)
  "time"     : "7:30 PM" style (string or null)
  "duration" : hours as number (default 1, use 2 for shows/concerts)
  "location" : venue or address (string or null)
  "notes"    : one line (string or null)

Return {{"events": []}} if the email contains no event or appointment.
Return ONLY valid JSON, no other text.

Subject: {subject}
Email date: {email_date}
---
{body}"""


@dataclass
class Event:
    title: str
    date: str
    time: Optional[str]
    duration_hours: float
    location: Optional[str]
    description: Optional[str]
    source_subject: str
    source_account: str


def decode_header_str(raw: str) -> str:
    parts = email.header.decode_header(raw or "")
    out = ""
    for part, charset in parts:
        if isinstance(part, bytes):
            out += part.decode(charset or "utf-8", errors="replace")
        else:
            out += str(part)
    return out


def _strip_html(html: str) -> str:
    import html as html_lib
    html = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = html_lib.unescape(text)
    # drop bare URLs (tracking links, unsubscribe links)
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"\s{3,}", "\n", text).strip()


def parse_emlx(path: Path) -> Optional[dict]:
    try:
        with open(path, "rb") as f:
            n = int(f.readline().strip())
            raw = f.read(n)
        msg = email.message_from_bytes(raw)

        subject = decode_header_str(msg.get("Subject", ""))
        try:
            date = email.utils.parsedate_to_datetime(msg.get("Date", ""))
        except Exception:
            return None

        plain_body = ""
        html_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain" and not plain_body:
                    cs = part.get_content_charset() or "utf-8"
                    try:
                        plain_body = part.get_payload(decode=True).decode(cs, errors="replace")
                    except Exception:
                        pass
                elif ct == "text/html" and not html_body:
                    cs = part.get_content_charset() or "utf-8"
                    try:
                        html = part.get_payload(decode=True).decode(cs, errors="replace")
                        html_body = _strip_html(html)
                    except Exception:
                        pass
            # prefer HTML (richer, already cleaned); fall back to plain
            body = html_body or plain_body
        else:
            cs = msg.get_content_charset() or "utf-8"
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    raw_body = payload.decode(cs, errors="replace")
                    if msg.get_content_type() == "text/html":
                        body = _strip_html(raw_body)
                    else:
                        body = raw_body
            except Exception:
                pass

        sender = decode_header_str(msg.get("From", ""))
        return {"subject": subject, "date": date, "body": body[:2000], "from": sender}
    except Exception:
        return None


def normalize_subject(subject: str) -> str:
    """Strip Re:/Fwd:/Fw: prefixes for thread dedup."""
    return re.sub(r"^(Re|Fwd?|AW|WG):\s*", "", subject, flags=re.IGNORECASE).strip().lower()


def dedup_threads(candidates: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """Keep only the earliest email per (account, normalized subject) thread."""
    seen: dict[tuple[str, str], tuple[dict, str]] = {}
    for data, account in candidates:
        key = (account, normalize_subject(data["subject"]))
        existing = seen.get(key)
        if existing is None or data["date"] < existing[0]["date"]:
            seen[key] = (data, account)
    return list(seen.values())


BLOCKED_SENDERS = [
    r"bandsintown\.com",  # matches @bandsintown.com and @updates.bandsintown.com
    r"@songkick\.com",
    r"@dice\.fm",
]


def prefilter(data: dict) -> bool:
    subject = data["subject"]
    sender = data.get("from", "")
    if re.match(r"^(Canceled|Cancelled):", subject, re.IGNORECASE):
        return False
    if any(re.search(p, sender, re.IGNORECASE) for p in BLOCKED_SENDERS):
        return False
    snippet = f"{subject} {data['body'][:600]}"
    if any(re.search(p, snippet, re.IGNORECASE) for p in NOISE_PATTERNS):
        return False
    has_event = any(re.search(p, snippet, re.IGNORECASE) for p in EVENT_SIGNALS)
    has_date = any(re.search(p, snippet, re.IGNORECASE) for p in DATE_SIGNALS)
    return has_event and has_date


def extract_events(data: dict, account: str) -> list[Event]:
    today = datetime.now().strftime("%Y-%m-%d")
    email_date = data["date"].strftime("%Y-%m-%d")
    prompt = EXTRACTION_PROMPT.format(
        today=today,
        email_date=email_date,
        subject=data["subject"],
        body=data["body"],
    )
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            },
            timeout=60,
        )
        if resp.status_code != 200:
            return []
        raw = resp.json().get("response", "")
        parsed = json.loads(raw)
        # model sometimes wraps in {"events": [...]}
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    parsed = v
                    break
            else:
                return []
        if not isinstance(parsed, list):
            return []

        def _str(v) -> Optional[str]:
            if v is None:
                return None
            return str(v) if not isinstance(v, str) else v

        events = []
        for e in parsed:
            if not e.get("title") or not e.get("date"):
                continue
            events.append(Event(
                title=_str(e["title"]) or "",
                date=_str(e["date"]) or "",
                time=_str(e.get("time")),
                duration_hours=float(e.get("duration") or 1),
                location=_str(e.get("location")),
                description=_str(e.get("notes")),
                source_subject=data["subject"],
                source_account=account,
            ))
        return events
    except Exception:
        return []


def group_duplicates(events: list[Event]) -> list[list[Event]]:
    groups: list[list[Event]] = []
    used: set[int] = set()
    for i, a in enumerate(events):
        if i in used:
            continue
        group = [a]
        used.add(i)
        for j, b in enumerate(events):
            if j in used:
                continue
            title_score = fuzz.token_sort_ratio(a.title, b.title)
            if title_score >= 85 and a.date == b.date:
                group.append(b)
                used.add(j)
        groups.append(group)
    return groups


SKIP_CALENDARS = {
    "birthdays", "holidays in united states", "holidays",
    "scheduled reminders", "default", "siri suggestions",
}

def get_calendars() -> list[tuple[str, str]]:
    """Return list of (calendar_name, account_email) tuples, filtered to writable ones."""
    script = '''
tell application "Calendar"
    set output to ""
    repeat with cal in calendars
        set calName to name of cal
        try
            set acct to username of account of cal
        on error
            try
                set acct to name of account of cal
            on error
                set acct to ""
            end try
        end try
        set output to output & calName & "|" & acct & "\n"
    end repeat
    return output
end tell'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        return []
    calendars = []
    seen = set()
    for line in result.stdout.strip().splitlines():
        if "|" not in line:
            continue
        name, acct = line.split("|", 1)
        name, acct = name.strip(), acct.strip()
        if name.lower() in SKIP_CALENDARS:
            continue
        key = (name.lower(), acct.lower())
        if key in seen:
            continue
        seen.add(key)
        calendars.append((name, acct))
    return calendars


def guess_calendar(account: str, calendars: list[tuple[str, str]]) -> str:
    """Pick the most likely calendar name for a given source account email."""
    names = [name for name, _ in calendars]
    # match by calendar account field first (most reliable)
    for name, cal_acct in calendars:
        if account and cal_acct and account.lower() in cal_acct.lower():
            return name
    # fallback: noahbaxt -> Personal
    if account == "noahbaxt@gmail.com":
        for name in names:
            if name.lower() == "personal":
                return name
    local = account.split("@")[0].lower()
    for name in names:
        if local in name.lower():
            return name
    for name in names:
        if "personal" in name.lower():
            return name
    return names[0] if names else "Calendar"


def cal_choices(calendars: list[tuple[str, str]]) -> list[questionary.Choice]:
    return [
        questionary.Choice(f"{name}  ({acct})" if acct else name, value=name)
        for name, acct in calendars
    ]


def _clean_location(loc: Optional[str]) -> Optional[str]:
    if not loc:
        return None
    # Replace bullets, em/en dashes, middots with a comma-space
    loc = re.sub(r"\s*[•·—–]\s*", ", ", loc)
    # Collapse multiple commas/spaces
    loc = re.sub(r",\s*,", ",", loc)
    loc = re.sub(r"\s{2,}", " ", loc).strip().strip(",").strip()
    return loc or None


def _parse_time_seconds(time_str: Optional[str]) -> int:
    """Convert '7:00 PM' to seconds from midnight. Defaults to noon."""
    if not time_str:
        return 12 * 3600
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)?", time_str.strip(), re.IGNORECASE)
    if not m:
        return 12 * 3600
    h, mins = int(m.group(1)), int(m.group(2))
    period = (m.group(3) or "").upper()
    if period == "PM" and h != 12:
        h += 12
    elif period == "AM" and h == 12:
        h = 0
    return h * 3600 + mins * 60


def geocode(location: str) -> Optional[tuple[float, float]]:
    """Return (lat, lon) via Nominatim, or None on failure."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": location, "format": "json", "limit": 1},
            headers={"User-Agent": "mail-to-calendar/1.0"},
            timeout=5,
        )
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def event_exists(title: str, date_str: str, cal_name: str) -> bool:
    y, m, d = date_str.split("-")
    title_safe = title.replace('"', "'")[:40]
    script = f'''\
tell application "Calendar"
    set cal to first calendar whose name is "{cal_name}"
    set d1 to date "{m}/{d}/{y}"
    set time of d1 to 0
    set d2 to d1 + (86400)
    set found to (every event of cal whose start date >= d1 and start date < d2)
    repeat with ev in found
        if summary of ev contains "{title_safe}" then
            return "yes"
        end if
    end repeat
    return "no"
end tell'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.stdout.strip() == "yes"


SWIFT_HELPER = Path(__file__).parent / "add_event.swift"


def add_event(event: Event, cal_name: str):
    clean_loc = _clean_location(event.location) or ""
    time_secs = _parse_time_seconds(event.time)
    dur_secs  = int(event.duration_hours * 3600)
    notes     = (event.description or "").replace("\n", " ")

    geo = geocode(clean_loc) if clean_loc else None
    lat = str(geo[0]) if geo else ""
    lon = str(geo[1]) if geo else ""

    result = subprocess.run(
        [
            "swift", str(SWIFT_HELPER),
            cal_name, event.title, event.date,
            str(time_secs), str(dur_secs),
            clean_loc, lat, lon, notes,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != "ok":
        console.print(f"[red]  Failed: {(result.stderr or result.stdout).strip()}[/red]")
    else:
        loc_note = f" @ {clean_loc}" if clean_loc else ""
        console.print(f"[green]  Added '{event.title}'{loc_note} -> {cal_name}[/green]")


def main():
    console.rule("[bold]mail-to-calendar[/bold]")

    # Verify Ollama is up
    try:
        requests.get("http://localhost:11434", timeout=3)
    except Exception:
        console.print("[red]Ollama is not running. Start it with: ollama serve[/red]")
        sys.exit(1)

    calendars: list[tuple[str, str]] = get_calendars()
    if not calendars:
        console.print("[red]Could not read calendars. Grant Terminal Full Disk + Automation access.[/red]")
        sys.exit(1)

    # --- inbox selection ---
    account_choices = [
        questionary.Choice(f"{email}  ({uuid[:8]}...)", value=uuid)
        for uuid, email in ACCOUNT_MAP.items()
        if (MAIL_BASE / uuid).exists()
    ]
    selected_uuids = questionary.checkbox(
        "Which inboxes to scan?",
        choices=account_choices,
        instruction="(space to toggle, enter to confirm)",
    ).ask()
    if not selected_uuids:
        console.print("[yellow]Nothing selected.[/yellow]")
        return
    selected_accounts = {uuid: ACCOUNT_MAP[uuid] for uuid in selected_uuids}

    # Ask once per selected account which calendar to use for it
    account_calendar_map: dict[str, str] = {}
    for account in selected_accounts.values():
        default = guess_calendar(account, calendars)
        choices = cal_choices(calendars)
        chosen = questionary.select(
            f"Default calendar for {account}?",
            choices=choices,
            default=default,
        ).ask()
        if chosen:
            account_calendar_map[account] = chosen

    # --- lookback ---
    days_choice = questionary.select(
        "How far back?",
        choices=[
            questionary.Choice("1 week", value=7),
            questionary.Choice("1 month", value=30),
            questionary.Choice("3 months", value=90),
            questionary.Choice("6 months", value=180),
            questionary.Choice("Custom...", value=0),
        ],
    ).ask()
    if days_choice == 0:
        raw = questionary.text("Number of days:").ask()
        try:
            days_choice = int(raw)
        except (ValueError, TypeError):
            console.print("[red]Invalid number.[/red]")
            return

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_choice)

    # Scan inboxes
    candidates: list[tuple[dict, str]] = []
    for uuid, account in selected_accounts.items():
        acct_dir = MAIL_BASE / uuid
        if not acct_dir.exists():
            continue
        for emlx in acct_dir.rglob("*.emlx"):
            data = parse_emlx(emlx)
            if not data or not data["date"]:
                continue
            msg_date = data["date"]
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date < cutoff:
                continue
            if prefilter(data):
                candidates.append((data, account))

    candidates = dedup_threads(candidates)
    console.print(f"\n{len(candidates)} candidate email(s) after thread dedup\n")
    if not candidates:
        console.print("[yellow]Nothing to process.[/yellow]")
        return

    # Extract events via Ollama in parallel
    all_events: list[Event] = []
    completed = 0
    total = len(candidates)

    def process(item: tuple[dict, str]) -> list[Event]:
        data, account = item
        return extract_events(data, account)

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(process, item): item for item in candidates}
        for future in as_completed(futures):
            data, account = futures[future]
            completed += 1
            events = future.result() or []
            if events:
                for ev in events:
                    console.print(f"[dim]({completed}/{total})[/dim] [green]+[/green] {ev.title} | {ev.date} {ev.time or ''}")
            else:
                console.print(f"[dim]({completed}/{total}) {data['subject'][:60]}[/dim]")
            all_events.extend(events)

    if not all_events:
        console.print("\n[yellow]No events found.[/yellow]")
        return

    groups = group_duplicates(all_events)
    console.print(f"\n[bold]{len(all_events)}[/bold] event(s), [bold]{len(groups)}[/bold] unique after dedup\n")

    # Summary table
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("#", width=3)
    table.add_column("Title")
    table.add_column("Date", width=12)
    table.add_column("Time", width=10)
    table.add_column("Location")
    table.add_column("Account")
    for i, group in enumerate(groups, 1):
        ev = group[0]
        dup = f" [dim](x{len(group)})[/dim]" if len(group) > 1 else ""
        table.add_row(str(i), ev.title + dup, ev.date, ev.time or "", ev.location or "", ev.source_account)
    console.print(table)
    console.print()

    # Interactive picker
    to_add: list[tuple[Event, str]] = []
    for group in groups:
        if len(group) == 1:
            ev = group[0]
            default_cal = guess_calendar(ev.source_account, calendars)
            label = f"{ev.title} | {ev.date} {ev.time or ''} | {ev.source_account}"
            if not questionary.confirm(f"Add: {label}?", default=True).ask():
                continue
        else:
            console.print(f"\n[yellow]Duplicate group ({len(group)} sources):[/yellow]")
            choices = [
                questionary.Choice(
                    f"{ev.title} | {ev.date} {ev.time or ''} | {ev.source_account} | \"{ev.source_subject[:40]}\"",
                    value=ev,
                )
                for ev in group
            ]
            choices.append(questionary.Choice("Skip", value=None))
            ev = questionary.select("Which version?", choices=choices).ask()
            if not ev:
                continue
            default_cal = guess_calendar(ev.source_account, calendars)

        cal = account_calendar_map.get(ev.source_account) or guess_calendar(ev.source_account, calendars)
        if event_exists(ev.title, ev.date, cal):
            console.print(f"[yellow]  Skipping '{ev.title}' on {ev.date} — already exists in {cal}[/yellow]")
            continue
        to_add.append((ev, cal))

    if not to_add:
        console.print("\n[yellow]Nothing selected.[/yellow]")
        return

    console.print(f"\nAdding {len(to_add)} event(s)...\n")
    for ev, cal in to_add:
        add_event(ev, cal)

    console.print("\n[bold green]Done.[/bold green]")


if __name__ == "__main__":
    main()
