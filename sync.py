import os
import json
import hashlib
import random
import re
import time
import unicodedata
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BAK_URL = os.getenv("BAK_URL", "https://gyarab.bakalari.cz").rstrip("/")
BASE_URL = f"{BAK_URL}/api/3"
ROOM_MAPPING_FILE = os.path.join(os.path.dirname(__file__), "room_mapping.json")

CANCELLED_WORDS = (
    "zruš",
    "zrus",
    "cancel",
    "odpad",
    "r"removed",emoved",
    "vyjmut",
    "volno",
)

SPECIAL_EVENT_WORDS = (
    "exkurz",
    "výlet",
    "vylet",
    "projekt",
    "olympiád",
    "olympiad",
    "akce",
    "besed",
    "koncert",
    "divadl",
    "kurz",
    "přednáš",
    "prednas",
)


def normalize_room_name(value):
    """Normalizuje názvy učeben pro porovnání různých variant."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def load_room_mapping(path=ROOM_MAPPING_FILE):
    """Načte upravitelnou mapu názvů učeben z JSON souboru."""
    with open(path, encoding="utf-8") as mapping_file:
        config = json.load(mapping_file)

    mapping = {}
    for room in config.get("rooms", []):
        number = str(room["number"])
        for alias in room.get("aliases", []):
            normalized_alias = normalize_room_name(alias)
            if normalized_alias:
                mapping[normalized_alias] = number
    return mapping


def convert_room_name(room, room_mapping):
    """Vrátí číslo učebny, nebo původní název, pokud v mapě není."""
    return room_mapping.get(normalize_room_name(room), room)


def get_bakalari_token(user, password):
    response = requests.post(
        f"{BAK_URL}/api/login",
        data={
            "client_id": "ANDR",
            "grant_type": "password",
            "username": user,
            "password": password,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if "access_token" not in data:
        raise RuntimeError(f"Bakaláři nevrátil access_token. Odpověď: {data}")
    return data["access_token"]


def normalize_time(value):
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    # Bakaláři sometimes returns minutes since midnight, e.g. 465 -> 07:45
    if text.isdigit():
        total_minutes = int(text)
        if 0 <= total_minutes < 24 * 60:
            hours, minutes = divmod(total_minutes, 60)
            return f"{hours:02d}:{minutes:02d}"
        return None

    if ":" not in text:
        return None

    parts = text.split(":")
    if len(parts) != 2:
        return None

    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        if not (0 <= hours < 24 and 0 <= minutes < 60):
            return None
    except ValueError:
        return None

    return f"{hours:02d}:{minutes:02d}"


def extract_hour_times(item):
    start = (
        item.get("StartTime")
        or item.get("Start")
        or item.get("TimeFrom")
        or item.get("From")
    )
    end = (
        item.get("EndTime")
        or item.get("End")
        or item.get("TimeTo")
        or item.get("To")
    )
    start_norm = normalize_time(start)
    end_norm = normalize_time(end)
    if not start_norm or not end_norm:
        return None
    return start_norm, end_norm


def make_rfc3339(date_str, time_str):
    """Převede datum a čas do RFC3339 formátu s časovou zónou Europe/Prague."""
    normalized = normalize_time(time_str)
    if normalized is None:
        raise ValueError(
            f"Neplatný čas z Bakaláři API: {time_str!r} "
            f"(datum {date_str!r})"
        )

    local_dt = datetime.strptime(
        f"{date_str}T{normalized}",
        "%Y-%m-%dT%H:%M",
    ).replace(tzinfo=ZoneInfo("Europe/Prague"))
    return local_dt.isoformat()


def _absence_status(item):
    """Vrátí text stavu absence z různých verzí API Bakalářů."""
    for key in ("IsExcused", "Excused", "Omluveno"):
        value = item.get(key)
        if isinstance(value, bool):
            return "omluveno" if value else "neomluveno"

    value = item.get("Type") or item.get("TypeName") or item.get("Status")
    if isinstance(value, dict):
        value = value.get("Name") or value.get("Description") or value.get("Id")
    text = str(value or "").strip().lower()
    if any(word in text for word in ("omluv", "excused", "justif")):
        return "omluveno"
    return "neomluveno"


def extract_absences(data):
    """Vytvoří mapu (datum, HourId) -> stav absence."""
    result = {}
    records = data.get("Absences", data.get("Data", []))
    if isinstance(records, dict):
        records = records.get("Items", records.get("Absences", []))
    if not isinstance(records, list):
        return result

    for item in records:
        if not isinstance(item, dict):
            continue
        date_value = item.get("Date") or item.get("Datum") or item.get("Day")
        hour_id = item.get("HourId")
        if date_value is None or hour_id is None:
            continue
        date_str = str(date_value)[:10]
        result[(date_str, str(hour_id))] = _absence_status(item)
    return result


def _atom_text(atom, subject):
    change = atom.get("Change")
    values = [subject]
    if isinstance(change, dict):
        values.extend(
            str(change.get(key, ""))
            for key in (
                "Description",
                "Name",
                "Type",
                "TypeName",
                "ChangeType",
                "TypeAbbrev",
            )
        )
    values.extend(
        str(atom.get(key, ""))
        for key in (
            "Description",
            "Name",
            "Type",
            "TypeName",
            "ChangeType",
            "TypeAbbrev",
        )
    )
    return " ".join(values).strip()


def _special_title(atom, subject):
    """Vrátí název vícehodinové akce, nebo None pro běžnou hodinu."""
    text = _atom_text(atom, subject)
    folded = text.casefold()
    if not any(word in folded for word in SPECIAL_EVENT_WORDS):
        return None

    change = atom.get("Change")
    candidates = []
    if isinstance(change, dict):
        candidates.extend(change.get(key) for key in ("Description", "Name", "TypeAbbrev"))
    candidates.extend(atom.get(key) for key in ("Description", "Name"))
    candidates.append(subject)

    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and not any(word in value.casefold() for word in CANCELLED_WORDS):
            return value
    return subject


def _is_cancelled(atom, subject):
    if not isinstance(atom, dict):
        return False

    change = atom.get("Change")
    if isinstance(change, dict):
        change_type = str(change.get("ChangeType") or "").strip().casefold()
        if change_type in ("canceled", "cancelled", "removed"):
            return True
        if atom.get("SubjectId") is None and not _special_title(atom, subject):
            return True

    text = _atom_text(atom, subject).casefold()
    return any(word in text for word in CANCELLED_WORDS)


def _change_description(atom):
    change = atom.get("Change")
    if isinstance(change, dict):
        return str(
            change.get("Description")
            or change.get("Name")
            or change.get("TypeAbbrev")
            or ""
        ).strip()
    return ""


def extract_teacher_name(atom, teachers=None):
    """Vrátí jméno vyučujícího z dat hodiny, pokud je dostupné."""
    if not isinstance(atom, dict):
        return None

    teachers = teachers or {}

    for key in (
        "Teacher",
        "TeacherName",
        "TeacherShortName",
        "TeacherDisplayName",
        "TeacherAbbrev",
        "TeacherLabel",
        "TeacherFullName",
    ):
        value = atom.get(key)
        if isinstance(value, dict):
            for nested_key in (
                "Name",
                "FullName",
                "DisplayName",
                "ShortName",
                "Abbrev",
                "Title",
            ):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str):
                    nested_value = nested_value.strip()
                    if nested_value:
                        return nested_value
        if isinstance(value, str):
            value = value.strip()
            if value:
                return value

    teacher_id = atom.get("TeacherId")
    if teacher_id is not None:
        teacher_name = teachers.get(teacher_id)
        if isinstance(teacher_name, str):
            teacher_name = teacher_name.strip()
            if teacher_name:
                return teacher_name

    return None


def extract_class_name(atom, classes=None):
    """Vrátí jméno třídy z dat hodiny, pokud je dostupné."""
    if not isinstance(atom, dict):
        return None

    if classes is None:
        classes = {}

    for key in (
        "Class",
        "ClassName",
        "ClassTitle",
        "ClassDisplayName",
        "ClassLabel",
    ):
        value = atom.get(key)
        if isinstance(value, dict):
            for nested_key in ("Name", "Abbrev", "ShortName", "DisplayName", "Title"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str):
                    nested_value = nested_value.strip()
                    if nested_value:
                        return nested_value
        if isinstance(value, str):
            value = value.strip()
            if value:
                return value

    class_id = atom.get("ClassId")
    if class_id is not None and class_id in classes:
        class_name = classes[class_id]
        if isinstance(class_name, str):
            class_name = class_name.strip()
            if class_name:
                return class_name

    return None


def build_event_summary(subject, room, title=None):
    """Vytvoří název akce s číslem učebny v závorkách."""
    room_value = str(room or "").strip()
    base_title = str(title or subject or "").strip()
    if not base_title:
        return room_value or ""
    if room_value and f"({room_value})" not in base_title:
        return f"{base_title} ({room_value})"
    return base_title


def build_event_location(class_name, room, room_name=None):
    """Vytvoří lokaci akce: preferuje skutečný název učebny z Bakalářů, pak číslo učebny."""
    class_value = str(class_name or "").strip()
    room_name = str(room_name or "").strip()
    room_value = str(room or "").strip()
    if room_name:
        if room_value and room_name != room_value and room_value not in room_name:
            return f"{room_name} - {room_value}"
        return room_name
    if room_value:
        return room_value
    return class_value


def build_event_description(
    teacher_name=None,
    change_info="",
    absence_status=None,
    room_name=None,
    room_number=None,
):
    """Sestaví popis události bez duplicitní místnosti; lokace je uložena v poli location."""
    description_parts = []
    if teacher_name:
        description_parts.append(teacher_name)
    elif not change_info:
        description_parts.append("Běžná hodina")
    if change_info:
        description_parts.append(f"Změna/Suplování: {change_info}")
    if absence_status:
        description_parts.append(f"Absence: {absence_status}")
    return "\n".join(description_parts)


def _event_id(date_str, start_time, subject=None):
    """Generuje stabilní MD5 ID události čistě z data a času začátku."""
    raw_id = f"{date_str}_{start_time}"
    return hashlib.md5(raw_id.encode("utf-8")).hexdigest()


def _legacy_event_id(date_str, start_time, subject):
    """Původní výpočet ID události se jménem předmětu (pro zpětné promazání starých záznamů)."""
    normalized_subject = str(subject or "").strip()
    normalized_subject = re.sub(r"\s*\([^)]*\)\s*$", "", normalized_subject)
    raw_id = f"{date_str}_{start_time}_{normalized_subject}"
    return hashlib.md5(raw_id.encode("utf-8")).hexdigest()


def execute_with_backoff(request, attempts=6):
    """Retry transient Google Calendar errors with exponential backoff."""
    for attempt in range(attempts):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            reason = ""
            payload = getattr(exc, "content", None)
            if payload:
                reason = payload.decode("utf-8", errors="replace")
            retryable = (
                status in (429, 500, 502, 503, 504)
                or "rateLimitExceeded" in reason
                or "userRateLimitExceeded" in reason
                or "quotaExceeded" in reason
            )
            if not retryable or attempt == attempts - 1:
                raise
            delay = min(60, 2 ** attempt) + random.uniform(0, 1)
            print(
                "Google Calendar request was rate-limited; "
                f"retrying in {delay:.1f}s"
            )
            time.sleep(delay)
    raise RuntimeError("Google Calendar request failed without a result.")


def week_window(reference_date=None):
    """Vrátí pondělí aktuálního týdne a konec synchronizovaného okna (od pátku 2 týdny dopředu)."""
    if reference_date is None:
        reference_date = datetime.now(ZoneInfo("Europe/Prague")).date()
    week_start = reference_date - timedelta(days=reference_date.weekday())
    days_ahead = 21 if reference_date.weekday() >= 4 else 14
    return week_start, week_start + timedelta(days=days_ahead)


def cleanup_cutoff_date(reference_date=None):
    """Vrátí datum, před kterým se mažou staré týdny (předchozí týden se maže až v pátek)."""
    if reference_date is None:
        reference_date = datetime.now(ZoneInfo("Europe/Prague")).date()
    week_start = reference_date - timedelta(days=reference_date.weekday())
    if reference_date.weekday() >= 4:
        return week_start
    return week_start - timedelta(days=7)


def select_calendar_id(
    date_str, current_calendar_id, permanent_calendar_id, reference_date=None
):
    """Vybere kalendář pro aktuální nebo následující kalendářní týden."""
    if not current_calendar_id:
        return permanent_calendar_id
    if not permanent_calendar_id:
        return current_calendar_id
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return current_calendar_id
    week_start, _ = week_window(reference_date)
    if event_date >= week_start + timedelta(days=7):
        return permanent_calendar_id
    return current_calendar_id


def is_day_cancelled(day):
    """Vrátí True, pokud je daný den svátek nebo zrušený."""
    if not isinstance(day, dict):
        return False
    for key in (
        "IsCancelled",
        "Cancelled",
        "IsHoliday",
        "Holiday",
        "IsNonTeachingDay",
        "IsFreeDay",
        "IsOff",
    ):
        value = day.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, str):
            text = value.strip().casefold()
            if text in {
                "true",
                "yes",
                "ano",
                "svatek",
                "svátek",
                "zruseno",
                "zrušeno",
                "holiday",
            }:
                return True

    for key in ("Type", "TypeName", "Name", "DayType", "State", "DayDescription"):
        value = day.get(key)
        if isinstance(value, dict):
            value = (
                value.get("Name")
                or value.get("Description")
                or value.get("Type")
                or value.get("Code")
            )
        text = str(value or "").strip().casefold()
        if not text:
            continue
        if any(
            word in text
            for word in (
                "svátek",
                "svatek",
                "holiday",
                "day off",
                "dny off",
                "zrušeno",
                "zruseno",
                "cancelled",
                "canceled",
                "prázdniny",
                "prazdniny",
                "ředitelské volno",
                "reditelske volno",
            )
        ):
            return True
    return False


def event_color_for(change_info, title):
    """Vrátí barvu Google kalendáře pro typ události."""
    if title:
        return "2"
    return None


def _delete_event(service, calendar_id, event_id):
    try:
        execute_with_backoff(
            service.events().delete(calendarId=calendar_id, eventId=event_id)
        )
        return True
    except HttpError as error:
        if error.resp.status not in (404, 410):
            raise
        return False


def upsert_event(service, calendar_id, event_body):
    """Vloží nebo aktualizuje událost a ošetří případ, kdy byla dříve smazána (HTTP 409/410)."""
    body_with_status = {**event_body, "status": "confirmed"}
    event_id = body_with_status["id"]
    try:
        execute_with_backoff(
            service.events().insert(calendarId=calendar_id, body=body_with_status)
        )
        return "inserted", event_id
    except HttpError as error:
        if error.resp.status != 409:
            raise
        try:
            execute_with_backoff(
                service.events().update(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body=body_with_status,
                )
            )
            return "updated", event_id
        except HttpError as update_error:
            if update_error.resp.status not in (404, 410):
                raise
            fallback_id = hashlib.md5(
                f"{event_id}_restored".encode("utf-8")
            ).hexdigest()
            fallback_body = {**body_with_status, "id": fallback_id}
            try:
                execute_with_backoff(
                    service.events().insert(
                        calendarId=calendar_id, body=fallback_body
                    )
                )
                return "inserted", fallback_id
            except HttpError as fallback_error:
                if fallback_error.resp.status != 409:
                    raise
                execute_with_backoff(
                    service.events().update(
                        calendarId=calendar_id,
                        eventId=fallback_id,
                        body=fallback_body,
                    )
                )
                return "updated", fallback_id


def delete_expired_events(service, calendar_ids, week_start):
    """Odstraní události tohoto synchronizátoru před začátkem aktuálního týdne."""
    time_max = datetime.combine(
        week_start,
        datetime.min.time(),
        tzinfo=ZoneInfo("Europe/Prague"),
    ).isoformat()
    deleted = 0
    for calendar_id in dict.fromkeys(
        calendar_id for calendar_id in calendar_ids if calendar_id
    ):
        page_token = None
        while True:
            response = execute_with_backoff(
                service.events().list(
                    calendarId=calendar_id,
                    timeMax=time_max,
                    singleEvents=True,
                    maxResults=250,
                    pageToken=page_token,
                )
            )
            for event in response.get("items", []):
                event_id = event.get("id", "")
                if re.fullmatch(r"[0-9a-f]{32}", event_id):
                    if _delete_event(service, calendar_id, event_id):
                        deleted += 1
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return deleted


def delete_stale_window_events(
    service, calendar_ids, week_start, window_end, active_events_by_calendar
):
    """Odstraní z kalendářů v okně 14 dnů všechny události skriptu, které už nejsou v rozvrhu."""
    time_min = datetime.combine(
        week_start,
        datetime.min.time(),
        tzinfo=ZoneInfo("Europe/Prague"),
    ).isoformat()
    time_max = datetime.combine(
        window_end,
        datetime.min.time(),
        tzinfo=ZoneInfo("Europe/Prague"),
    ).isoformat()

    deleted = 0
    for calendar_id in dict.fromkeys(
        calendar_id for calendar_id in calendar_ids if calendar_id
    ):
        keep_ids = active_events_by_calendar.get(calendar_id, set())
        page_token = None
        while True:
            response = execute_with_backoff(
                service.events().list(
                    calendarId=calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    maxResults=250,
                    pageToken=page_token,
                )
            )
            for event in response.get("items", []):
                event_id = event.get("id", "")
                if re.fullmatch(r"[0-9a-f]{32}", event_id) and event_id not in keep_ids:
                    if _delete_event(service, calendar_id, event_id):
                        deleted += 1
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return deleted


def build_timetable_urls(date_str):
    """Vrátí pravděpodobné Bakaláři endpointy pro zvolený den / týden."""
    candidates = [
        f"{BASE_URL}/timetable/actual?date={date_str}",
        f"{BASE_URL}/timetable?date={date_str}",
        f"{BASE_URL}/timetable/actual?day={date_str}",
        f"{BASE_URL}/timetable/actual?from={date_str}&to={date_str}",
    ]
    if "-" in date_str:
        monday = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(
            days=datetime.strptime(date_str, "%Y-%m-%d").weekday()
        )
        sunday = monday + timedelta(days=6)
        candidates.extend(
            [
                f"{BASE_URL}/timetable/actual?from={monday.isoformat()}&to={sunday.isoformat()}",
                f"{BASE_URL}/timetable?from={monday.isoformat()}&to={sunday.isoformat()}",
            ]
        )
    return list(dict.fromkeys(candidates))


def fetch_timetable_for_date(headers, date_str):
    """Načte rozvrh pro den nebo týden z Bakalářů a vrátí JSON nebo None."""
    for url in build_timetable_urls(date_str):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            continue
        payload = response.json()
        if not isinstance(payload, dict):
            continue
        if (
            payload.get("Days")
            or payload.get("Hours")
            or payload.get("Subjects")
            or payload.get("Rooms")
        ):
            return payload
    return None


def should_sync_now(day_data, current_time=None):
    """Vrátí True, pokud je teď 20 minut po začátku vyučovací hodiny."""
    if current_time is None:
        current_dt = datetime.now(ZoneInfo("Europe/Prague"))
    else:
        current_dt = datetime.fromisoformat(current_time)
        if current_dt.tzinfo is None:
            current_dt = current_dt.replace(tzinfo=ZoneInfo("Europe/Prague"))
        current_dt = current_dt.astimezone(ZoneInfo("Europe/Prague"))

    if isinstance(day_data, dict):
        days = day_data.get("Days", [])
    else:
        days = day_data

    for day in days:
        if isinstance(day, dict):
            date_value = str(day.get("Date") or "")[:10]
            atoms = day.get("Atoms", [])
            if not date_value or date_value != current_dt.strftime("%Y-%m-%d"):
                continue
            if is_day_cancelled(day):
                continue
        else:
            date_value = current_dt.strftime("%Y-%m-%d")
            atoms = day_data if isinstance(day_data, list) else []

        for atom in atoms:
            if not isinstance(atom, dict):
                continue
            if atom.get("HourId") is None:
                continue
            start = (
                atom.get("StartTime")
                or atom.get("Start")
                or atom.get("TimeFrom")
                or atom.get("From")
            )
            if start is None:
                continue
            normalized_start = normalize_time(start)
            if normalized_start is None:
                continue
            try:
                lesson_start = datetime.strptime(
                    f"{date_value}T{normalized_start}", "%Y-%m-%dT%H:%M"
                ).replace(tzinfo=ZoneInfo("Europe/Prague"))
            except ValueError:
                continue
            if lesson_start + timedelta(minutes=20) <= current_dt:
                return True
    return False


def merge_timetable_data(base_data, extra_data):
    """Spojí rozvrhy z aktuálního a následujícího týdne do jednoho datasetu."""
    if not extra_data:
        return base_data

    for key in ("Days", "Hours", "Subjects", "Rooms", "Classes", "Teachers"):
        extra_items = extra_data.get(key, [])
        if not isinstance(extra_items, list):
            continue
        base_items = base_data.setdefault(key, [])
        if not isinstance(base_items, list):
            base_items = []
            base_data[key] = base_items

        if key == "Days":
            seen = {
                str(item.get("Date") or "")[:10]
                for item in base_items
                if isinstance(item, dict) and item.get("Date")
            }
            for item in extra_items:
                if not isinstance(item, dict):
                    continue
                date_key = str(item.get("Date") or "")[:10]
                if date_key and date_key in seen:
                    continue
                base_items.append(item)
                if date_key:
                    seen.add(date_key)
        else:
            seen = {
                item.get("Id")
                for item in base_items
                if isinstance(item, dict) and item.get("Id") is not None
            }
            for item in extra_items:
                if not isinstance(item, dict):
                    continue
                item_id = item.get("Id")
                if item_id is not None and item_id in seen:
                    continue
                base_items.append(item)
                if item_id is not None:
                    seen.add(item_id)
    return base_data


def group_special_event_atoms(atoms, start_index):
    """Spojí sousední části jedné vícedobové akce i přes přestávky."""
    atom, subject, hour_info = atoms[start_index]
    title = _special_title(atom, subject)
    if not title:
        return [(atom, subject, hour_info)], start_index + 1

    group = [(atom, subject, hour_info)]
    next_index = start_index + 1
    while next_index < len(atoms):
        next_atom, next_subject, next_info = atoms[next_index]
        if _is_cancelled(next_atom, next_subject):
            break
        if _special_title(next_atom, next_subject) != title:
            break
        group.append((next_atom, next_subject, next_info))
        next_index += 1
    return group, next_index


def main():
    user = os.environ["BAK_USER"]
    password = os.environ["BAK_PASS"]
    current_calendar_id = os.environ.get("CALENDAR_ID") or os.environ.get(
        "CURRENT_CALENDAR_ID"
    )
    permanent_calendar_id = os.environ.get("PERMANENT_CALENDAR_ID")
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    room_mapping = load_room_mapping()

    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    service = build("calendar", "v3", credentials=credentials)

    today = datetime.now(ZoneInfo("Europe/Prague")).date()
    monday, window_end = week_window(today)
    cleanup_before = cleanup_cutoff_date(today)
    expired_deleted = delete_expired_events(
        service,
        (current_calendar_id, permanent_calendar_id),
        cleanup_before,
    )

    token = get_bakalari_token(user, password)
    headers = {"Authorization": f"Bearer {token}"}

    data = fetch_timetable_for_date(headers, monday.isoformat())
    if data is None:
        response = requests.get(
            f"{BASE_URL}/timetable/actual",
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

    week_cursor = monday + timedelta(days=7)
    while week_cursor < window_end:
        next_data = fetch_timetable_for_date(headers, week_cursor.isoformat())
        if next_data is not None:
            data = merge_timetable_data(data, next_data)
        week_cursor += timedelta(days=7)

    if (
        os.environ.get("RESPECT_20MIN_RULE", "").lower() == "true"
        and not should_sync_now(data)
        and os.environ.get("FORCE_SYNC", "").lower() != "true"
    ):
        print("Synchronizace přeskočena: není 20 minut po začátku vyučovací hodiny.")
        return

    absence_response = requests.get(
        f"{BASE_URL}/absence/student",
        headers=headers,
        timeout=30,
        verify=False,
    )
    absence_response.raise_for_status()
    absences = extract_absences(absence_response.json())

    subjects = {
        item["Id"]: item.get("Abbrev", item.get("Name", "Hodina"))
        for item in data.get("Subjects", [])
        if item.get("Id") is not None
    }
    classes = {
        item["Id"]: item.get("Abbrev", item.get("Name", ""))
        for item in data.get("Classes", [])
        if item.get("Id") is not None
    }
    teachers = {
        item["Id"]: (
            item.get("Name")
            or item.get("FullName")
            or item.get("DisplayName")
            or item.get("Abbrev")
            or ""
        )
        for item in data.get("Teachers", [])
        if item.get("Id") is not None
    }
    room_names = {
        item["Id"]: (item.get("Name") or item.get("Abbrev") or "")
        for item in data.get("Rooms", [])
        if item.get("Id") is not None
    }
    rooms = {
        item["Id"]: convert_room_name(
            item.get("Abbrev", item.get("Name", "")), room_mapping
        )
        for item in data.get("Rooms", [])
        if item.get("Id") is not None
    }

    hours = {}
    for item in data.get("Hours", []):
        hour_id = item.get("Id")
        if hour_id is None:
            continue
        hour_times = extract_hour_times(item)
        if hour_times:
            hours[hour_id] = hour_times

    if not hours:
        sample = data.get("Hours", [])[:2]
        raise RuntimeError(
            "API nevrátilo správné časy vyučovacích hodin. "
            f"Ukázka dat Hours: {sample}"
        )

    inserted = 0
    updated = 0
    deleted = 0
    active_events_by_calendar = {}

    for day in data.get("Days", []):
        date_str = day.get("Date", "")[:10]
        if not monday.isoformat() <= date_str < window_end.isoformat():
            continue
        if is_day_cancelled(day):
            continue

        atoms = []
        for atom in day.get("Atoms", []):
            subject = subjects.get(atom.get("SubjectId"), "Hodina")
            hour_info = hours.get(atom.get("HourId"))
            if hour_info:
                atoms.append((atom, subject, hour_info))

        if not atoms:
            continue

        index = 0
        while index < len(atoms):
            atom, subject, hour_info = atoms[index]
            start_time, _ = hour_info
            title = _special_title(atom, subject)
            target_calendar_id = select_calendar_id(
                date_str, current_calendar_id, permanent_calendar_id, today
            )

            if _is_cancelled(atom, subject):
                if _delete_event(
                    service,
                    target_calendar_id,
                    _event_id(date_str, start_time, subject),
                ):
                    deleted += 1
                if _delete_event(
                    service,
                    target_calendar_id,
                    _legacy_event_id(date_str, start_time, subject),
                ):
                    deleted += 1
                index += 1
                continue

            group, next_index = group_special_event_atoms(atoms, index)
            if title:
                end_time = max(hour_data[1] for _, _, hour_data in group)
            else:
                end_time = hour_info[1]

            first_atom = group[0][0]
            room = rooms.get(first_atom.get("RoomId"), "")
            room_name = room_names.get(first_atom.get("RoomId"), room)
            class_name = extract_class_name(first_atom, classes)
            change_info = _change_description(first_atom)
            absence_status = absences.get((date_str, str(first_atom.get("HourId"))))
            teacher_name = extract_teacher_name(first_atom, teachers)

            summary = build_event_summary(subject, room, title)
            location = build_event_location(class_name, room, room_name)
            description = build_event_description(
                teacher_name=teacher_name,
                change_info=change_info,
                absence_status=absence_status,
                room_name=room_name,
                room_number=room,
            )

            event_id = _event_id(date_str, start_time, subject)

            event_body = {
                "id": event_id,
                "summary": summary,
                "description": description,
                "location": location,
                "start": {
                    "dateTime": make_rfc3339(date_str, start_time),
                    "timeZone": "Europe/Prague",
                },
                "end": {
                    "dateTime": make_rfc3339(date_str, end_time),
                    "timeZone": "Europe/Prague",
                },
            }
            color_id = event_color_for(change_info, title)
            if color_id is not None:
                event_body["colorId"] = color_id

            action, final_event_id = upsert_event(
                service, target_calendar_id, event_body
            )
            active_events_by_calendar.setdefault(target_calendar_id, set()).add(
                final_event_id
            )
            if action == "inserted":
                inserted += 1
            else:
                updated += 1

            index = next_index

    deleted += delete_stale_window_events(
        service,
        (current_calendar_id, permanent_calendar_id),
        monday,
        window_end,
        active_events_by_calendar,
    )

    print(
        "Synchronizace úspěšně dokončena. "
        f"Nové události: {inserted}, Aktualizované události: {updated}, "
        f"Odstraněné zrušené hodiny: {deleted}, "
        f"Odstraněné události minulých týdnů: {expired_deleted}"
    )


if __name__ == "__main__":
    main()
