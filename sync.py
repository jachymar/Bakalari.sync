import os
import json
import hashlib
import re
import unicodedata
import requests

from datetime import datetime
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
    """Převádí datum a čas do RFC3339 formátu s časovou zónou Europe/Prague."""
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
    """Vrátí český text stavu absence z různých verzí API Bakalářů."""
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
            for key in ("Description", "Name", "Type", "TypeName")
        )
    values.extend(
        str(atom.get(key, ""))
        for key in ("Description", "Name", "Type", "TypeName")
    )
    return " ".join(values).strip()


def _is_cancelled(atom, subject):
    text = _atom_text(atom, subject).casefold()
    return any(word in text for word in CANCELLED_WORDS)


def _special_title(atom, subject):
    """Vrátí název vícedohodinové akce, nebo None pro běžnou hodinu."""
    text = _atom_text(atom, subject)
    folded = text.casefold()
    if not any(word in folded for word in SPECIAL_EVENT_WORDS):
        return None

    change = atom.get("Change")
    candidates = []
    if isinstance(change, dict):
        candidates.extend(change.get(key) for key in ("Description", "Name"))
    candidates.extend(atom.get(key) for key in ("Description", "Name"))
    candidates.append(subject)
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and not any(word in value.casefold() for word in CANCELLED_WORDS):
            return value
    return subject


def _change_description(atom):
    change = atom.get("Change")
    if isinstance(change, dict):
        return str(change.get("Description") or change.get("Name") or "").strip()
    return ""


def _event_id(date_str, start_time, title):
    raw_id = f"{date_str}_{start_time}_{title}"
    return hashlib.md5(raw_id.encode("utf-8")).hexdigest()


def _delete_event(service, calendar_id, event_id):
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError as error:
        if error.resp.status != 404:
            raise


def main():
    user = os.environ["BAK_USER"]
    password = os.environ["BAK_PASS"]
    calendar_id = os.environ["CALENDAR_ID"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    room_mapping = load_room_mapping()

    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    service = build("calendar", "v3", credentials=credentials)

    token = get_bakalari_token(user, password)
    headers = {"Authorization": f"Bearer {token}"}

    response = requests.get(
        f"{BASE_URL}/timetable/actual",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

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

    for day in data.get("Days", []):
        date_str = day["Date"][:10]
        atoms = []
        for atom in day.get("Atoms", []):
            subject = subjects.get(atom.get("SubjectId"), "Hodina")
            hour_info = hours.get(atom.get("HourId"))
            if hour_info:
                atoms.append((atom, subject, hour_info))

        index = 0
        while index < len(atoms):
            atom, subject, hour_info = atoms[index]
            start_time, end_time = hour_info
            title = _special_title(atom, subject)

            if _is_cancelled(atom, subject):
                _delete_event(service, calendar_id, _event_id(date_str, start_time, subject))
                deleted += 1
                index += 1
                continue

            group = [(atom, subject, hour_info)]
            if title:
                next_index = index + 1
                while next_index < len(atoms):
                    next_atom, next_subject, next_info = atoms[next_index]
                    if _is_cancelled(next_atom, next_subject):
                        break
                    next_title = _special_title(next_atom, next_subject)
                    if next_title != title or next_info[0] != group[-1][2][1]:
                        break
                    group.append((next_atom, next_subject, next_info))
                    next_index += 1
                end_time = group[-1][2][1]
            else:
                next_index = index + 1

            first_atom = group[0][0]
            room = rooms.get(first_atom.get("RoomId"), "")
            change_info = _change_description(first_atom)
            absence_status = absences.get((date_str, str(first_atom.get("HourId"))))
            description_parts = []
            if change_info:
                description_parts.append(f"Změna/Suplování: {change_info}")
            else:
                description_parts.append("Běžná hodina")
            if absence_status:
                description_parts.append(f"Absence: {absence_status}")

            summary = title or (f"{subject} ({room})" if room else subject)
            event_id = _event_id(date_str, start_time, summary)
            event_body = {
                "id": event_id,
                "summary": summary,
                "description": "\n".join(description_parts),
                "location": room,
                "start": {"dateTime": make_rfc3339(date_str, start_time), "timeZone": "Europe/Prague"},
                "end": {"dateTime": make_rfc3339(date_str, end_time), "timeZone": "Europe/Prague"},
            }
            if change_info:
                event_body["colorId"] = "6"  # Google Calendar: tangerine/oranžová
            elif absence_status:
                event_body["colorId"] = "11"

            try:
                service.events().insert(calendarId=calendar_id, body=event_body).execute()
                inserted += 1
            except HttpError as error:
                if error.resp.status != 409:
                    raise
                service.events().update(calendarId=calendar_id, eventId=event_id, body=event_body).execute()
                updated += 1

            index += len(group)

    print(
        "Synchronizace úspěšně dokončena. "
        f"Nové události: {inserted}, Aktualizované události: {updated}, "
        f"Odstraněné zrušené hodiny: {deleted}"
    )


if __name__ == "__main__":
    main()
