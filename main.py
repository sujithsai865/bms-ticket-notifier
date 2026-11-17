
import os
import re
import sys
import json
import requests

from html import escape
from datetime import datetime
from dataclasses import dataclass, field
from urllib.parse import urlparse


# ============================================================
# CONFIGURATION
# ============================================================

CONFIG = {
    "url": os.getenv("BMS_URL", ""),
    "dates": os.getenv("BMS_DATES", ""),
    "theatre": os.getenv("BMS_THEATRE", ""),
    "time_period": os.getenv("BMS_TIME", ""),
}

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_TO_EMAIL = os.getenv("RESEND_TO_EMAIL", "")
RESEND_FROM_EMAIL = os.getenv(
    "RESEND_FROM_EMAIL", "onboarding@resend.dev"
)

STATE_FILE = "bms_state.json"

API_URL = (
    "https://in.bookmyshow.com/api/movies-data/v4/"
    "showtimes-by-event/primary-dynamic"
)


AVAIL_STATUS_MAP = {
    "0": ("SOLD OUT", "🔴"),
    "1": ("ALMOST FULL", "🟡"),
    "2": ("FILLING FAST", "🟠"),
    "3": ("AVAILABLE", "🟢"),
}

DATE_STYLE_MAP = {
    "date-selected": "BOOKABLE",
    "date-disabled": "NOT_OPEN",
    "date-default": "AVAILABLE",
}

TIME_PERIODS = {
    "morning": (600, 1200),
    "afternoon": (1200, 1600),
    "evening": (1600, 1900),
    "night": (1900, 2400),
}

REGION_MAP = {
    "chennai": ("CHEN", "chennai", "13.056", "80.206", "tf3"),
    "mumbai": ("MUMBAI", "mumbai", "19.076", "72.878", "te7"),
    "delhi-ncr": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "delhi": ("NCR", "delhi-ncr", "28.613", "77.209", "ttn"),
    "bengaluru": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "bangalore": ("BANG", "bengaluru", "12.972", "77.594", "tdr"),
    "hyderabad": ("HYD", "hyderabad", "17.385", "78.487", "tep"),
    "kolkata": ("KOLK", "kolkata", "22.573", "88.364", "tun"),
    "pune": ("PUNE", "pune", "18.520", "73.856", "te2"),
    "kochi": ("KOCH", "kochi", "9.932", "76.267", "t9z"),
}


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class CatInfo:
    name: str
    price: str
    status: str


@dataclass
class ShowInfo:
    venue_code: str
    venue_name: str
    session_id: str
    date_code: str
    time: str
    time_code: str
    screen_attr: str
    categories: list = field(default_factory=list)


@dataclass
class DateInfo:
    date_code: str
    status: str


# ============================================================
# URL AND REGION
# ============================================================

def parse_bms_url(url):
    path = urlparse(url).path.strip("/")
    parts = path.split("/")

    result = {
        "event_code": None,
        "date_code": None,
        "region_slug": None,
    }

    for part in parts:
        if re.match(r"^ET\d{8,}$", part):
            result["event_code"] = part

        elif re.match(r"^\d{8}$", part):
            result["date_code"] = part

    if "movies" in parts:
        index = parts.index("movies")

        if index + 1 < len(parts):
            result["region_slug"] = parts[index + 1]

    return result


def resolve_region(slug):
    key = (slug or "").lower().strip()

    if key in REGION_MAP:
        return REGION_MAP[key]

    raise ValueError(
        f"Unsupported BookMyShow region: {slug}"
    )


# ============================================================
# BOOKMYSHOW REQUEST
# ============================================================

class BMSFetchError(Exception):
    pass


def fetch_bms(
    event_code,
    date_code,
    region_code,
    region_slug,
    lat,
    lon,
    geohash
):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": (
            f"https://in.bookmyshow.com/movies/"
            f"{region_slug}/buytickets/{event_code}/"
        ),
        "x-app-code": "WEB",
        "x-region-code": region_code,
        "x-region-slug": region_slug,
        "x-geohash": geohash,
        "x-latitude": lat,
        "x-longitude": lon,
        "x-location-selection": "manual",
        "x-lsid": "",
    }

    params = {
        "eventCode": event_code,
        "dateCode": date_code or "",
        "isDesktop": "true",
        "regionCode": region_code,
        "xLocationShared": "false",
        "memberId": "",
        "lsId": "",
        "subCode": "",
        "lat": lat,
        "lon": lon,
    }

    try:
        response = requests.get(
            API_URL,
            headers=headers,
            params=params,
            timeout=15,
        )

        print(f"  HTTP status: {response.status_code}")
        print(
            "  Content-Type:",
            response.headers.get("Content-Type", "Unknown")
        )

        if response.status_code != 200:
            # Limit diagnostic output; don't print cookies or credentials.
            print(
                "  Response preview:",
                response.text[:500]
            )

            raise BMSFetchError(
                f"BookMyShow returned HTTP {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError:
            raise BMSFetchError(
                "BookMyShow returned invalid JSON."
            )

        if not isinstance(data, dict):
            raise BMSFetchError(
                "Unexpected response format."
            )

        if not isinstance(data.get("data"), dict):
            raise BMSFetchError(
                "Response does not contain the expected data object."
            )

        return data

    except requests.RequestException as error:
        raise BMSFetchError(
            f"Network request failed: {error}"
        )


# ============================================================
# RESPONSE PARSERS
# ============================================================

def parse_movie_info(data):
    info = {
        "name": "Unknown Movie",
        "language": "",
    }

    for widget in data.get("data", {}).get(
        "topStickyWidgets", []
    ):
        if widget.get("type") == "horizontal-text-list":

            for item in widget.get("data", []):

                for row in item.get(
                    "leftText", {}
                ).get("data", []):

                    for component in row.get(
                        "components", []
                    ):

                        if "•" in component.get("text", ""):
                            info["language"] = (
                                component["text"].strip()
                            )

    bottom_sheet = data.get(
        "data", {}
    ).get("bottomSheetData", {})

    for widget in bottom_sheet.get(
        "format-selector", {}
    ).get("widgets", []):

        if widget.get("type") == "vertical-text-list":

            for item in widget.get("data", []):

                if item.get("styleId") == "bottomsheet-subtitle":
                    info["name"] = item.get(
                        "text", info["name"]
                    )

    return info


def parse_dates(data):
    dates = []

    for widget in data.get("data", {}).get(
        "topStickyWidgets", []
    ):

        if widget.get("type") != "horizontal-block-list":
            continue

        for item in widget.get("data", []):

            if len(item.get("data", [])) >= 3:
                style = item.get("styleId", "")

                dates.append(
                    DateInfo(
                        date_code=item.get("id", ""),
                        status=DATE_STYLE_MAP.get(
                            style, "UNKNOWN"
                        ),
                    )
                )

    return dates


def parse_shows(data):
    shows = []

    for widget in data.get("data", {}).get(
        "showtimeWidgets", []
    ):

        if widget.get("type") != "groupList":
            continue

        for group in widget.get("data", []):

            if group.get("type") != "venueGroup":
                continue

            for card in group.get("data", []):

                if card.get("type") != "venue-card":
                    continue

                additional = card.get(
                    "additionalData", {}
                )

                venue_name = additional.get(
                    "venueName", "Unknown"
                )

                venue_code = additional.get(
                    "venueCode", ""
                )

                for showtime in card.get(
                    "showtimes", []
                ):

                    show_data = showtime.get(
                        "additionalData", {}
                    )

                    date_code = str(
                        show_data.get("showDateCode", "")
                        or show_data.get("dateCode", "")
                    ).strip()

                    cutoff = show_data.get(
                        "cutOffDateTime", ""
                    )

                    if not date_code and re.match(
                        r"^\d{8}", cutoff
                    ):
                        date_code = cutoff[:8]

                    show = ShowInfo(
                        venue_code=venue_code,
                        venue_name=venue_name,
                        session_id=show_data.get(
                            "sessionId", ""
                        ),
                        date_code=date_code,
                        time=showtime.get("title", ""),
                        time_code=str(
                            show_data.get("showTimeCode", "")
                        ),
                        screen_attr=(
                            showtime.get("screenAttr", "")
                            or show_data.get("attributes", "")
                        ),
                    )

                    for category in show_data.get(
                        "categories", []
                    ):

                        show.categories.append(
                            CatInfo(
                                name=category.get(
                                    "priceDesc", ""
                                ),
                                price=str(
                                    category.get("curPrice", "0")
                                ),
                                status=str(
                                    category.get("availStatus", "")
                                ),
                            )
                        )

                    shows.append(show)

    return shows


# ============================================================
# FILTERING
# ============================================================

def get_time_number(show):
    """
    Supports numeric HHMM values and HH:MM showtime strings.
    Uses the show title if the API time code isn't usable.
    """

    value = str(show.time_code).strip()

    if re.fullmatch(r"\d{3,4}", value):
        return int(value)

    match = re.search(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)?",
        show.time,
        re.IGNORECASE
    )

    if match:
        hour = int(match.group(1))
        minute = int(match.group(2))
        period = (match.group(3) or "").upper()

        if period == "PM" and hour != 12:
            hour += 12
        elif period == "AM" and hour == 12:
            hour = 0

        return hour * 100 + minute

    return None


def filter_shows(
    shows,
    theatre_filter,
    time_periods,
    date_codes
):
    result = []

    keywords = [
        item.strip().lower()
        for item in theatre_filter.split(",")
        if item.strip()
    ] if theatre_filter else []

    periods = [
        item.strip().lower()
        for item in time_periods.split(",")
        if item.strip()
    ] if time_periods else []

    dates_set = set(
        item.strip()
        for item in date_codes.split(",")
        if item.strip()
    ) if date_codes else set()

    for show in shows:

        # Theatre filter
        if keywords:
            venue = show.venue_name.lower()

            if not any(
                keyword in venue
                for keyword in keywords
            ):
                continue

        # Date filter
        if dates_set:
            if show.date_code not in dates_set:
                continue

        # Time filter
        if periods:
            time_number = get_time_number(show)

            if time_number is None:
                continue

            matched = any(
                period in TIME_PERIODS
                and TIME_PERIODS[period][0]
                <= time_number
                < TIME_PERIODS[period][1]
                for period in periods
            )

            if not matched:
                continue

        result.append(show)

    return result


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            content = file.read().strip()

        if not content:
            return {}

        state = json.loads(content)

        if not isinstance(state, dict):
            raise ValueError("State must be a JSON object.")

        return state

    except FileNotFoundError:
        return {}

    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"State file contains invalid JSON: {error}"
        )


def save_state(state):
    temporary_file = STATE_FILE + ".tmp"

    with open(
        temporary_file, "w", encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False
        )

    os.replace(temporary_file, STATE_FILE)


def build_state(shows, dates):
    show_state = {}

    for show in shows:

        for category in show.categories:

            key = (
                f"{show.venue_code}|"
                f"{show.session_id}|"
                f"{show.date_code}|"
                f"{category.name}"
            )

            show_state[key] = {
                "venue": show.venue_name,
                "time": show.time,
                "date": show.date_code,
                "cat": category.name,
                "price": category.price,
                "status": category.status,
            }

    date_state = {
        date.date_code: date.status
        for date in dates
        if date.date_code
    }

    return {
        "shows": show_state,
        "dates": date_state,
    }


def detect_changes(old_state, new_state):
    changes = []

    old_dates = old_state.get("dates", {})
    new_dates = new_state.get("dates", {})

    # Newly opened dates
    for date_code, status in new_dates.items():

        old_status = old_dates.get(date_code)

        if (
            old_status == "NOT_OPEN"
            and status in ("BOOKABLE", "AVAILABLE")
        ):
            changes.append(
                f"NEW DATE OPENED: {date_code}"
            )

    old_shows = old_state.get("shows", {})
    new_shows = new_state.get("shows", {})

    # New showtimes
    for key in set(new_shows) - set(old_shows):

        show = new_shows[key]

        changes.append(
            f"NEW SHOW: {show['venue']} "
            f"{show['time']} [{show['date']}] "
            f"- {show['cat']} Rs.{show['price']}"
        )

    # Availability changes
    for key, new_show in new_shows.items():

        old_show = old_shows.get(key)

        if not old_show:
            continue

        if (
            old_show["status"] == "0"
            and new_show["status"] != "0"
        ):

            label, emoji = AVAIL_STATUS_MAP.get(
                new_show["status"],
                ("UNKNOWN", "⚪")
            )

            changes.append(
                f"{emoji} BACK AVAILABLE: "
                f"{new_show['venue']} "
                f"{new_show['time']} "
                f"[{new_show['date']}] "
                f"- {new_show['cat']} -> {label}"
            )

        elif old_show["status"] != new_show["status"]:

            old_label = AVAIL_STATUS_MAP.get(
                old_show["status"], ("UNKNOWN", "")
            )[0]

            new_label = AVAIL_STATUS_MAP.get(
                new_show["status"], ("UNKNOWN", "")
            )[0]

            changes.append(
                f"AVAILABILITY UPDATE: "
                f"{new_show['venue']} "
                f"{new_show['time']} "
                f"[{new_show['date']}] "
                f"- {new_show['cat']}: "
                f"{old_label} -> {new_label}"
            )

    return changes


# ============================================================
# EMAIL NOTIFICATION
# ============================================================

def send_email(subject, changes, shows, movie_info):
    api_key = RESEND_API_KEY.strip()
    recipient = RESEND_TO_EMAIL.strip()
    sender = RESEND_FROM_EMAIL.strip()

    if not api_key or not recipient or not sender:
        raise RuntimeError(
            "Missing RESEND_API_KEY, RESEND_TO_EMAIL "
            "or RESEND_FROM_EMAIL."
        )

    now = datetime.now().strftime(
        "%d %b %Y, %I:%M %p"
    )

    movie_name = movie_info.get(
        "name", "Movie"
    )

    change_items = "".join(
        f"<li>{escape(change)}</li>"
        for change in changes
    )

    venue_groups = {}

    for show in shows:
        venue_groups.setdefault(
            show.venue_name, []
        ).append(show)

    shows_html = ""

    for venue, venue_shows in venue_groups.items():

        rows = ""

        for show in venue_shows:

            categories = " | ".join(
                f"{escape(category.name)} "
                f"Rs.{escape(category.price)} "
                f"({AVAIL_STATUS_MAP.get(category.status, ('UNKNOWN', ''))[0]})"
                for category in show.categories
            )

            rows += f"""
            <tr>
                <td>{escape(show.time)}</td>
                <td>{categories}</td>
            </tr>
            """

        shows_html += f"""
        <h3>{escape(venue)}</h3>
        <table border="1" cellpadding="6"
               cellspacing="0" width="100%">
            <tr>
                <th>Time</th>
                <th>Categories</th>
            </tr>
            {rows}
        </table>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family:Arial;color:#333;">
        <h2>BMS Alert: {escape(movie_name)}</h2>
        <p>Checked at: {escape(now)}</p>

        <h3>Changes Detected</h3>
        <ul>{change_items}</ul>

        <h3>Current Showtimes</h3>
        {shows_html}

        <p>Automated BMS Ticket Notifier</p>
    </body>
    </html>
    """

    plain_lines = [
        f"BMS Alert: {movie_name}",
        f"Checked at: {now}",
        "",
        "Changes detected:",
    ]

    plain_lines.extend(
        f"- {change}" for change in changes
    )

    plain_lines.append("")
    plain_lines.append("Current showtimes:")

    for venue, venue_shows in venue_groups.items():

        plain_lines.append(f"\n{venue}")

        for show in venue_shows:

            categories = " | ".join(
                f"{category.name} Rs.{category.price} "
                f"({AVAIL_STATUS_MAP.get(category.status, ('UNKNOWN', ''))[0]})"
                for category in show.categories
            )

            plain_lines.append(
                f"{show.time}: {categories}"
            )

    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "text": "\n".join(plain_lines),
                "html": html,
            },
            timeout=15,
        )

        if response.status_code not in (200, 201):
            print(
                f"Resend error: HTTP {response.status_code}"
            )
            print("Response:", response.text[:500])

            raise RuntimeError(
                "Email delivery request failed."
            )

        print(f"Email accepted by Resend for {recipient}")

    except requests.RequestException as error:
        raise RuntimeError(
            f"Email request failed: {error}"
        )


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():

    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        "BMS Ticket Checker — CI mode"
    )

    if not CONFIG["url"]:
        raise RuntimeError("BMS_URL is not configured.")

    parsed = parse_bms_url(CONFIG["url"])

    event_code = parsed["event_code"]
    region_slug = parsed["region_slug"]
    url_date = parsed["date_code"] or ""

    if not event_code or not region_slug:
        raise RuntimeError(
            "Invalid BMS_URL. Could not extract event or region."
        )

    (
        region_code,
        region_slug_resolved,
        lat,
        lon,
        geohash
    ) = resolve_region(region_slug)

    raw_dates = CONFIG["dates"].strip()

    if raw_dates:
        date_list = [
            date.strip()
            for date in raw_dates.split(",")
            if date.strip()
        ]

        if not all(
            re.fullmatch(r"\d{8}", date)
            for date in date_list
        ):
            raise RuntimeError(
                "BMS_DATES must contain YYYYMMDD dates."
            )

    elif url_date:
        date_list = [url_date]

    else:
        date_list = [""]

    print(
        f"Event: {event_code} | "
        f"Region: {region_code} | "
        f"Dates: {date_list}"
    )

    # Fetch every configured date.
    all_shows = []
    all_dates = []

    movie_info = {
        "name": "Unknown Movie",
        "language": "",
    }

    for date_code in date_list:

        print(f"\nChecking date: {date_code or 'Default'}")

        data = fetch_bms(
            event_code,
            date_code,
            region_code,
            region_slug_resolved,
            lat,
            lon,
            geohash
        )

        if movie_info["name"] == "Unknown Movie":
            movie_info = parse_movie_info(data)

        all_dates.extend(parse_dates(data))
        all_shows.extend(parse_shows(data))

    print(
        f"\nMovie: {movie_info['name']} "
        f"{movie_info['language']}"
    )

    print(f"Raw showtimes found: {len(all_shows)}")

    # Apply filters.
    filtered = filter_shows(
        all_shows,
        CONFIG["theatre"],
        CONFIG["time_period"],
        CONFIG["dates"]
    )

    print(
        f"Showtimes after filters: {len(filtered)}"
    )

    # A successful response with no shows is valid.
    # We can still record available date information.
    new_state = build_state(filtered, all_dates)

    old_state = load_state()

    changes = []

    # First successful check establishes baseline.
    is_first_run = not bool(old_state)

    if not is_first_run:
        changes = detect_changes(
            old_state,
            new_state
        )

    # Send notifications before saving the new state.
    # If email delivery fails, the old state remains intact
    # so the change can be detected on the next run.
    if is_first_run:
        print(
            "\nFirst successful check: establishing baseline."
        )

    elif changes:

        print(f"\n{len(changes)} change(s) detected:")

        for change in changes:
            print(f" - {change}")

        send_email(
            f"BMS Alert: {movie_info['name']} "
            f"- {len(changes)} change(s)",
            changes,
            filtered,
            movie_info
        )

    else:
        print("\nNo changes since previous check.")

    # Save only after all requests have succeeded.
    save_state(new_state)

    print("\nState file updated successfully.")

    print(
        f"\nCurrent showtimes ({len(filtered)}):"
    )

    for show in filtered:

        categories = ", ".join(
            f"{category.name}=Rs.{category.price}"
            f"({AVAIL_STATUS_MAP.get(category.status, ('UNKNOWN', ''))[0]})"
            for category in show.categories
        )

        print(
            f"{show.venue_name} | "
            f"{show.time} | "
            f"{show.date_code} | "
            f"{categories}"
        )

    print("\nBMS Ticket Checker completed.")


if __name__ == "__main__":

    try:
        main()

    except (BMSFetchError, RuntimeError, ValueError) as error:
        print(f"\nERROR: {error}")
        sys.exit(1)
