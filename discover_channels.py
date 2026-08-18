"""
Telegram channel auditor.

Telegram publishes NO channel-search API, so a channel can only be added by its
exact @username -- guessing plausible names does not work. (When this system
was built, 60+ obvious guesses were tested: most did not exist, and most of the
ones that did had been dormant for years or posted medical rather than IT jobs.)

So when you find a channel worth following -- for example one a colleague
recommends -- paste it here and this tool will tell you whether it is worth the
request budget, judging three things the eye cannot check quickly:

    FRESHNESS  how long since the newest post
    RELEVANCE  density of IT/telecom vocabulary in recent posts
    NOISE      whether it is actually a medical/labour/dropshipping channel

    python discover_channels.py @channel1 @channel2
    python discover_channels.py @channel1 --add       # write it into config.yml
    python discover_channels.py --audit-config        # re-check what you use
"""

from __future__ import annotations

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

import http_client
from config import ROOT
from scrapers.base import parse_date

CONFIG_PATH = ROOT / "config.yml"

IT_TERMS = re.compile(
    r"\b(it|voip|sip|pbx|asterisk|issabel|freepbx|network|software|developer|"
    r"engineer|technical support|help ?desk|service desk|sysadmin|odoo|erp|"
    r"python|telecom|programmer|devops|data|cyber|linux|windows server|noc|"
    r"cisco|firewall|application support|system admin|"
    r"مبرمج|شبكات|دعم فني|تقنية معلومات|مهندس|برمجة|اتصالات)\b",
    re.I,
)
OFF_TOPIC = re.compile(
    r"\b(nurse|doctor|pharmac|dental|physician|midwife|"
    r"labour|labor|mason|carpenter|welder|driver|cleaner|housemaid|"
    r"طبيب|صيدل|تمريض|ممرض|سائق|عامل|نجار|حداد)\b",
    re.I,
)

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m"
)
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = BOLD = RESET = ""


@dataclass(slots=True)
class Audit:
    username: str
    exists: bool = False
    title: str = ""
    subscribers: str = ""
    posts: int = 0
    age_days: int = 9999
    it_hits: int = 0
    off_topic_hits: int = 0
    error: str = ""

    @property
    def verdict(self) -> str:
        if not self.exists:
            return "DEAD"
        if self.age_days > 30:
            return "DORMANT"
        if self.off_topic_hits > max(3, self.it_hits * 1.5):
            return "WRONG NICHE"
        if self.it_hits >= 3:
            return "RECOMMENDED"
        if self.age_days <= 7:
            return "USABLE"
        return "LOW SIGNAL"

    @property
    def good(self) -> bool:
        return self.verdict in ("RECOMMENDED", "USABLE")

    def colour(self) -> str:
        return {
            "RECOMMENDED": GREEN, "USABLE": GREEN,
            "LOW SIGNAL": YELLOW, "WRONG NICHE": YELLOW,
        }.get(self.verdict, RED)


def audit_channel(username: str) -> Audit:
    username = username.strip().lstrip("@").strip("/")
    if not username:
        return Audit(username="(blank)", error="empty username")
    if username.startswith("+") or "joinchat" in username:
        return Audit(
            username=username,
            error="private invite link -- the web preview cannot read it; "
                  "use telegram_api.py with a Telethon session instead.",
        )

    result = Audit(username=username)
    html = http_client.get_text(f"https://t.me/s/{username}", timeout=25)
    if not html.strip():
        result.error = "no response from t.me"
        return result

    soup = BeautifulSoup(html, "lxml")
    wraps = soup.select(".tgme_widget_message_wrap")
    title_el = soup.select_one('meta[property="og:title"]')
    counter = soup.select_one(".tgme_header_counter")
    result.title = (title_el.get("content", "") if title_el else "")[:44]
    result.subscribers = counter.get_text(strip=True) if counter else ""

    if not wraps:
        result.error = (
            "no public posts -- the channel is private, empty, has the web "
            "preview disabled, or the username is wrong"
        )
        return result

    result.exists = True
    result.posts = len(wraps)

    newest: datetime | None = None
    for wrap in wraps:
        time_el = wrap.select_one("time")
        if not time_el:
            continue
        when = parse_date(time_el.get("datetime"))
        if when and (newest is None or when > newest):
            newest = when
    if newest:
        result.age_days = max(
            0, (datetime.now(timezone.utc) - newest).days
        )

    blob = " ".join(
        el.get_text(" ", strip=True)
        for el in soup.select(".tgme_widget_message_text")
    )
    result.it_hits = len(IT_TERMS.findall(blob))
    result.off_topic_hits = len(OFF_TOPIC.findall(blob))
    return result


def render(audits: list[Audit]) -> None:
    print()
    print(f"  {'CHANNEL':<24}{'VERDICT':<14}{'LAST':<8}{'IT':<5}{'OFF':<5}{'SUBS':<18}TITLE")
    print("  " + "-" * 96)
    for a in sorted(audits, key=lambda x: (not x.good, x.age_days, -x.it_hits)):
        age = "-" if a.age_days >= 9999 else f"{a.age_days}d"
        print(
            f"  {a.colour()}{a.username[:23]:<24}{a.verdict:<14}{RESET}"
            f"{age:<8}{a.it_hits:<5}{a.off_topic_hits:<5}"
            f"{a.subscribers[:17]:<18}{a.title}"
        )
        if a.error:
            print(f"  {DIM}    -> {a.error}{RESET}")
    print()
    print(f"  {DIM}LAST = days since the newest post | IT = IT/telecom keyword hits")
    print(f"  OFF = off-topic (medical/labour) keyword hits{RESET}\n")


def add_to_config(usernames: list[str], path: Path = CONFIG_PATH) -> bool:
    """Insert channels into the telegram.channels list, preserving comments."""
    if not usernames:
        return False
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    try:
        start = next(
            i for i, line in enumerate(lines) if re.match(r"^\s*channels:\s*$", line)
        )
    except StopIteration:
        print(f"{RED}  Could not find `channels:` under `telegram:` in config.yml.{RESET}")
        return False

    existing = set()
    insert_at = start + 1
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("- "):
            existing.add(stripped[2:].split("#")[0].strip().lower())
            insert_at = i + 1
        elif stripped.startswith("#") or not stripped:
            continue
        else:
            break  # dedented out of the list

    added = [u for u in usernames if u.lower() not in existing]
    if not added:
        print(f"{YELLOW}  Already present in config.yml -- nothing to add.{RESET}")
        return False

    for offset, username in enumerate(added):
        lines.insert(insert_at + offset, f"    - {username}"
                                         f"{' ' * max(1, 20 - len(username))}# added by discover_channels.py")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{GREEN}  Added to config.yml: {', '.join(added)}{RESET}\n")
    return True


def channels_from_config(path: Path = CONFIG_PATH) -> list[str]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [str(c) for c in (data.get("telegram", {}).get("channels") or [])]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit Telegram job channels for freshness and IT relevance.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("channels", nargs="*", help="@usernames to audit")
    parser.add_argument("--add", action="store_true",
                        help="write channels that pass the audit into config.yml")
    parser.add_argument("--audit-config", action="store_true",
                        help="re-audit the channels already in config.yml")
    args = parser.parse_args()

    targets = list(args.channels)
    if args.audit_config:
        targets.extend(channels_from_config())
    if not targets:
        parser.print_help()
        return 1

    seen, unique = set(), []
    for t in targets:
        key = t.lstrip("@").lower()
        if key not in seen:
            seen.add(key)
            unique.append(t)

    print(f"\n{BOLD}  Auditing {len(unique)} Telegram channel(s)...{RESET}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        audits = list(pool.map(audit_channel, unique))
    render(audits)

    good = [a.username for a in audits if a.good]
    if args.add:
        if good:
            add_to_config(good)
        else:
            print(f"{RED}  Nothing passed the audit -- config.yml untouched.{RESET}\n")
    elif good:
        print(f"  {DIM}Add the {len(good)} usable channel(s) with:{RESET}")
        print(f"    python discover_channels.py {' '.join('@' + g for g in good)} --add\n")

    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
