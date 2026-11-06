import os
import json
import hashlib
import random
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "removed",
    "vyjmut",
    "volno",
    "absence of class",
    "nepřítomnost",
    "nepritomnost",
)
SPECIAL_EVENT_WORDS = (
    "exkurz",
    "exk",
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
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def load_room_mapping(path=ROOM_MAPPING_FILE):
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
    return room_mapping.get(normalize_room_name(room), room)


def get_bakalari_token(user, password, bak_url=None):
    target_url = (bak_url or BAK_URL).rstrip("/")
    response = requests.post(
        f"{target_url}/api/login",
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
        item.get("BeginTime")
        or item.get("StartTime")
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
    normalized = normalize_time(time_str)
    if normalized is None:
        raise ValueError(f"Neplatný čas z Bakaláři API: {time_str!r} (datum {date_str!r})")
    local_dt = datetime.strptime(
        f"{date_str}T{normalized}",
        "%Y-%m-%dT%H:%M",
    ).replace(tzinfo=ZoneInfo("Europe/Prague"))
    return local_dt.isoformat()


def _absence_status(item):
    value = item.get("Type") or item.get("TypeName") or item.get("Status")
    if isinstance(value, dict):
        value = value.get("Name") or value.get("Description") or value.get("Id")
    text = str(value or "").strip().lower()
    
    if any(word in text for word in ("škol", "skol", "repre", "soutěž", "soutez", "akce", "exkurz", "kurz")):
        return "skolni"

    for key in ("IsExcused", "Excused", "Omluveno"):
        is_excused = item.get(key)
        if isinstance(is_excused, bool):
            return "omluveno" if is_excused else "neomluveno"

    if any(word in text for word in ("omluv", "excused", "justif")):
        return "omluveno"
    return "neomluveno"


def extract_absences(data):
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


def fetch_daily_absences_by_subject(user_base_url, headers, date_str):
    try:
        resp = requests.get(
            f"{user_base_url}/absence/student?from={date_str}&to={date_str}",
            headers=headers,
            timeout=30,
            verify=False
        )
        if not resp.ok:
            return {}
        data = resp.json()
        
        day_agg = data.get("Absences", [{}])
        day_agg = day_agg[0] if day_agg else {}
        
        unsolved = day_agg.get("Unsolved", 0)
        school = day_agg.get("School", 0)
        
        default_status = "omluveno"
        if unsolved > 0:
            default_status = "neomluveno"
        elif school > 0:
            default_status = "skolni"
            
        abs_map = {}
        for subj in data.get("AbsencesPerSubject", []):
            name = subj.get("SubjectName")
            if not name:
                continue
            if subj.get("Base", 0) > 0 or subj.get("Late", 0) > 0:
                abs_map[name] = default_status
            elif subj.get("School", 0) > 0:
                abs_map[name] = "skolni"
        return abs_map
    except Exception as e:
        print(f"Chyba při stahování detailu absence pro {date_str}: {e}")
        return {}


def _atom_text(atom, subject):
    change = atom.get("Change")
    values = [subject]
    if isinstance(change, dict):
        values.extend(
            str(change.get(key, ""))
            for key in ("Description", "Name", "Type", "TypeName", "ChangeType", "TypeAbbrev")
        )
    values.extend(
        str(atom.get(key, ""))
        for key in ("Description", "Name", "Type", "TypeName", "ChangeType", "TypeAbbrev")
    )
    return " ".join(values).strip()


def _special_title(atom, subject):
    text = _atom_text(atom, subject)
    folded = text.casefold()
    if not any(word in folded for word in SPECIAL_EVENT_WORDS):
        return None
    change = atom.get("Change")
    candidates = []
    if isinstance(change, dict):
        candidates.extend(change.get(key) for key in ("TypeName", "Description", "Name", "TypeAbbrev"))
    candidates.extend(atom.get(key) for key in ("Description", "Name", "TypeName", "TypeAbbrev"))
    candidates.append(subject)
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        val_folded = value.casefold()
        if any(word in val_folded for word in CANCELLED_WORDS):
            continue
        if any(word in val_folded for word in SPECIAL_EVENT_WORDS):
            if val_folded == "exk":
                return "Exkurze"
            return value
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value and not any(word in value.casefold() for word in CANCELLED_WORDS):
            if value.casefold() == "exk":
                return "Exkurze"
            return value
    return "Exkurze" if str(subject or "").strip().casefold() == "exk" else subject


def _is_cancelled(atom, subject):
    if not isinstance(atom, dict):
        return False
    if _special_title(atom, subject):
        return False
    change = atom.get("Change")
    if isinstance(change, dict):
        change_type = str(change.get("ChangeType") or "").strip().casefold()
        if change_type in ("canceled", "cancelled", "removed"):
            return True
        if atom.get("SubjectId") is None:
            return True
    text = _atom_text(atom, subject).casefold()
    return any(word in text for word in CANCELLED_WORDS)


def _change_description(atom):
    change = atom.get("Change")
    if isinstance(change, dict):
        return str(
            change.get("Description") or change.get("Name") or change.get("TypeAbbrev") or ""
        ).strip()
    return ""


def extract_teacher_name(atom, teachers=None):
    if not isinstance(atom, dict):
        return None
    teachers = teachers or {}
    for key in ("Teacher", "TeacherName", "TeacherShortName", "TeacherDisplayName", "TeacherAbbrev", "TeacherLabel", "TeacherFullName"):
        value = atom.get(key)
        if isinstance(value, dict):
            for nested_key in ("Name", "FullName", "DisplayName", "ShortName", "Abbrev", "Title"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str) and nested_value.strip():
                    return nested_value.strip()
        if isinstance(value, str) and value.strip():
            return value.strip()
    teacher_id = atom.get("TeacherId")
    if teacher_id is not None:
        teacher_name = teachers.get(teacher_id)
        if isinstance(teacher_name, str) and teacher_name.strip():
            return teacher_name.strip()
    return None


def extract_class_name(atom, classes=None):
    if not isinstance(atom, dict):
        return None
    classes = classes or {}
    for key in ("Class", "ClassName", "ClassTitle", "ClassDisplayName", "ClassLabel"):
        value = atom.get(key)
        if isinstance(value, dict):
            for nested_key in ("Name", "Abbrev", "ShortName", "DisplayName", "Title"):
                nested_value = value.get(nested_key)
                if isinstance(nested_value, str) and nested_value.strip():
                    return nested_value.strip()
        if isinstance(value, str) and value.strip():
            return value.strip()
    class_id = atom.get("ClassId")
    if class_id is not None and class_id in classes:
        class_name = classes[class_id]
        if isinstance(class_name, str) and class_name.strip():
            return class_name.strip()
    return None


def build_event_summary(subject, room, title=None):
    room_value = str(room or "").strip()
    base_title = str(title or subject or "").strip()
    if not base_title:
        return room_value or ""
    if room_value and f"({room_value})" not in base_title:
        return f"{base_title} ({room_value})"
    return base_title


def build_event_location(class_name, room, room_name=None):
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


def build_event_description(teacher_name=None, change_info="", absence_status=None, room_name=None, room_number=None):
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
    raw_id = f"{date_str}_{start_time}"
    return hashlib.md5(raw_id.encode("utf-8")).hexdigest()


def execute_with_backoff(request, attempts=6):
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
            print(f"Google Calendar request was rate-limited; retrying in {delay:.1f}s")
            time.sleep(delay)
    raise RuntimeError("Google Calendar request failed without a result.")


def week_window(reference_date=None):
    if reference_date is None:
        reference_date = datetime.now(ZoneInfo("Europe/Prague")).date()
    week_start = reference_date - timedelta(days=reference_date.weekday())
    days_ahead = 21 if reference_date.weekday() >= 4 else 14
    return week_start, week_start + timedelta(days=days_ahead)


def cleanup_cutoff_date(reference_date=None):
    if reference_date is None:
        reference_date = datetime.now(ZoneInfo("Europe/Prague")).date()
    week_start = reference_date - timedelta(days=reference_date.weekday())
    # Smazání minulého týdne se provádí vždy od úderu nového pondělí. (Pátek minulý týden nemaže).
    return week_start - timedelta(days=7)


def select_calendar_id(date_str, current_calendar_id, permanent_calendar_id, reference_date=None):
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


def determine_target_calendar(date_str, current_cal, perm_cal, archive_cal, today, main_start, main_end):
    try:
        event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    if main_start <= event_date < main_end:
        return select_calendar_id(date_str, current_cal, perm_cal, today)
    return archive_cal


def is_day_cancelled(day):
    if not isinstance(day, dict):
        return False
    for key in ("IsCancelled", "Cancelled", "IsHoliday", "Holiday", "IsNonTeachingDay", "IsFreeDay", "IsOff"):
        value = day.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, str):
            text = value.strip().casefold()
            if text in {"true", "yes", "ano", "svatek", "svátek", "zruseno", "zrušeno", "holiday"}:
                return True
    for key in ("Type", "TypeName", "Name", "DayType", "State", "DayDescription"):
        value = day.get(key)
        if isinstance(value, dict):
            value = value.get("Name") or value.get("Description") or value.get("Type") or value.get("Code")
        text = str(value or "").strip().casefold()
        if not text:
            continue
        if any(word in text for word in (
            "svátek", "svatek", "holiday", "day off", "dny off", "zrušeno", 
            "zruseno", "cancelled", "canceled", "prázdniny", "prazdniny", 
            "ředitelské volno", "reditelske volno"
        )):
            return True
    return False


def event_color_for(change_info, title, absence_status=None):
    if absence_status in ("omluveno", "neomluveno"):
        return "4"
    if absence_status == "skolni":
        return "7"
    if title:
        return "2"
    return "8"


def _delete_event(service, calendar_id, event_id):
    try:
        execute_with_backoff(service.events().delete(calendarId=calendar_id, eventId=event_id))
        return True
    except HttpError as error:
        if getattr(error.resp, "status", None) not in (404, 410):
            raise
        return False


def smart_upsert_event(service, calendar_id, event_body, is_existing):
    body_with_status = {**event_body, "status": "confirmed"}
    event_id = body_with_status["id"]
    try:
        if is_existing:
            execute_with_backoff(service.events().update(calendarId=calendar_id, eventId=event_id, body=body_with_status))
            return "updated", event_id
        else:
            execute_with_backoff(service.events().insert(calendarId=calendar_id, body=body_with_status))
            return "inserted", event_id
    except HttpError as error:
        status = getattr(error.resp, "status", None)
        if status == 409 and not is_existing:
            execute_with_backoff(service.events().update(calendarId=calendar_id, eventId=event_id, body=body_with_status))
            return "updated", event_id
        elif status in (404, 410):
            fallback_id = hashlib.md5(f"{event_id}_restored".encode("utf-8")).hexdigest()
            fallback_body = {**body_with_status, "id": fallback_id}
            try:
                execute_with_backoff(service.events().insert(calendarId=calendar_id, body=fallback_body))
                return "inserted", fallback_id
            except HttpError as fallback_error:
                if getattr(fallback_error.resp, "status", None) == 409:
                    execute_with_backoff(service.events().update(calendarId=calendar_id, eventId=fallback_id, body=fallback_body))
                    return "updated", fallback_id
                raise
        raise


def fetch_existing_events(service, calendar_ids, time_min, time_max):
    """Stáhne do paměti všechny události z Googlu ve zvoleném okně."""
    existing = {}
    t_min = datetime.combine(time_min, datetime.min.time(), tzinfo=ZoneInfo("Europe/Prague")).isoformat()
    t_max = datetime.combine(time_max, datetime.min.time(), tzinfo=ZoneInfo("Europe/Prague")).isoformat()
    
    for cal_id in set(c for c in calendar_ids if c):
        existing[cal_id] = {}
        page_token = None
        while True:
            response = execute_with_backoff(
                service.events().list(
                    calendarId=cal_id,
                    timeMin=t_min,
                    timeMax=t_max,
                    singleEvents=True,
                    maxResults=2500,
                    pageToken=page_token
                )
            )
            for ev in response.get("items", []):
                if re.fullmatch(r"[0-9a-f]{32}(_restored)?", ev.get("id", "")):
                    existing[cal_id][ev["id"]] = ev
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return existing


def is_event_unchanged(existing_ev, event_body):
    """Porovná paměťový objekt z Googlu s tím, co chceme zapsat."""
    if not existing_ev:
        return False
    if existing_ev.get("summary", "") != event_body.get("summary", ""):
        return False
    if existing_ev.get("description", "") != event_body.get("description", ""):
        return False
    if existing_ev.get("location", "") != event_body.get("location", ""):
        return False
    if existing_ev.get("colorId") != event_body.get("colorId"):
        return False
    if existing_ev.get("start", {}).get("dateTime") != event_body.get("start", {}).get("dateTime"):
        return False
    if existing_ev.get("end", {}).get("dateTime") != event_body.get("end", {}).get("dateTime"):
        return False
    return True


def delete_expired_events(service, calendar_ids, cutoff_date):
    """Odstraní staré události v hluboké historii."""
    time_max = datetime.combine(cutoff_date, datetime.min.time(), tzinfo=ZoneInfo("Europe/Prague")).isoformat()
    deleted = 0
    for calendar_id in set(c for c in calendar_ids if c):
        page_token = None
        while True:
            response = execute_with_backoff(
                service.events().list(
                    calendarId=calendar_id,
                    timeMax=time_max,
                    singleEvents=True,
                    maxResults=2500,
                    pageToken=page_token,
                )
            )
            for event in response.get("items", []):
                event_id = event.get("id", "")
                if re.fullmatch(r"[0-9a-f]{32}(_restored)?", event_id):
                    if _delete_event(service, calendar_id, event_id):
                        deleted += 1
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    return deleted


def build_timetable_urls(date_str, base_url=None):
    target_base = (base_url or BASE_URL).rstrip("/")
    candidates = [
        f"{target_base}/timetable/actual?date={date_str}",
        f"{target_base}/timetable?date={date_str}",
        f"{target_base}/timetable/actual?day={date_str}",
        f"{target_base}/timetable/actual?from={date_str}&to={date_str}",
    ]
    if "-" in date_str:
        monday = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(
            days=datetime.strptime(date_str, "%Y-%m-%d").weekday()
        )
        sunday = monday + timedelta(days=6)
        candidates.extend([
            f"{target_base}/timetable/actual?from={monday.isoformat()}&to={sunday.isoformat()}",
            f"{target_base}/timetable?from={monday.isoformat()}&to={sunday.isoformat()}",
        ])
    return list(dict.fromkeys(candidates))


def fetch_timetable_for_date(headers, date_str, base_url=None):
    for url in build_timetable_urls(date_str, base_url=base_url):
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            continue
        payload = response.json()
        if not isinstance(payload, dict):
            continue
        if payload.get("Days") or payload.get("Hours") or payload.get("Subjects") or payload.get("Rooms"):
            return payload
    return None


def should_sync_now(day_data, current_time=None):
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
            start = atom.get("BeginTime") or atom.get("StartTime") or atom.get("Start") or atom.get("TimeFrom") or atom.get("From")
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
            seen = {str(item.get("Date") or "")[:10] for item in base_items if isinstance(item, dict) and item.get("Date")}
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
            seen = {item.get("Id") for item in base_items if isinstance(item, dict) and item.get("Id") is not None}
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


def filter_and_sort_day_atoms(raw_atoms, subjects, hours):
    valid_items = []
    active_hours = set()
    for atom in raw_atoms:
        if not isinstance(atom, dict):
            continue
        hour_id = atom.get("HourId")
        hour_info = hours.get(hour_id)
        if not hour_info:
            continue
        subject = subjects.get(atom.get("SubjectId"), "Hodina")
        is_canc = _is_cancelled(atom, subject)
        if not is_canc:
            active_hours.add(hour_id)
        valid_items.append((atom, subject, hour_info, is_canc, hour_id))

    filtered = []
    for atom, subject, hour_info, is_canc, hour_id in valid_items:
        if is_canc and hour_id in active_hours:
            continue
        filtered.append((atom, subject, hour_info))
    filtered.sort(key=lambda item: (item[2][0], _is_cancelled(item[0], item[1])))
    return filtered


def group_special_event_atoms(atoms, start_index):
    atom, subject, hour_info = atoms[start_index]
    title = _special_title(atom, subject)
    if not title:
        return [(atom, subject, hour_info)], start_index + 1

    group = [(atom, subject, hour_info)]
    next_index = start_index + 1

    while next_index < len(atoms):
        next_atom, next_subject, next_info = atoms[next_index]

        if _is_cancelled(next_atom, next_subject):
            lookahead = next_index + 1
            while lookahead < len(atoms) and _is_cancelled(atoms[lookahead][0], atoms[lookahead][1]):
                lookahead += 1

            if lookahead < len(atoms) and _special_title(atoms[lookahead][0], atoms[lookahead][1]) == title:
                next_index = lookahead
                continue
            break

        if _special_title(next_atom, next_subject) != title:
            break

        group.append((next_atom, next_subject, next_info))
        next_index += 1

    return group, next_index


def _normalize_user_entry(raw_entry, index=1, default_bak_url=None):
    if not isinstance(raw_entry, dict):
        raise ValueError(f"Konfigurace uživatele #{index} musí být objekt (dict).")

    user = raw_entry.get("user") or raw_entry.get("username") or raw_entry.get("bak_user") or raw_entry.get("BAK_USER")
    password = raw_entry.get("password") or raw_entry.get("pass") or raw_entry.get("bak_pass") or raw_entry.get("BAK_PASS")
    current_cal = raw_entry.get("current_calendar_id") or raw_entry.get("calendar_id") or raw_entry.get("CALENDAR_ID") or raw_entry.get("CURRENT_CALENDAR_ID")
    permanent_cal = raw_entry.get("permanent_calendar_id") or raw_entry.get("PERMANENT_CALENDAR_ID")
    archive_cal = raw_entry.get("archive_calendar_id") or raw_entry.get("history_calendar_id") or raw_entry.get("ARCHIVE_CALENDAR_ID") or raw_entry.get("HISTORY_CALENDAR_ID")
    bak_url = (raw_entry.get("bak_url") or raw_entry.get("BAK_URL") or default_bak_url or BAK_URL).rstrip("/")
    name = raw_entry.get("name") or str(user or f"user_{index}")

    if not user or not password:
        raise ValueError(f"Uživatel '{name}' nemá vyplněné přihlašovací údaje (user/password).")
    if not current_cal and not permanent_cal:
        raise ValueError(f"Uživatel '{name}' nemá nastavený žádný hlavní kalendář.")

    return {
        "name": str(name),
        "user": str(user),
        "password": str(password),
        "current_calendar_id": str(current_cal) if current_cal else None,
        "permanent_calendar_id": str(permanent_cal) if permanent_cal else None,
        "archive_calendar_id": str(archive_cal) if archive_cal else None,
        "bak_url": bak_url,
    }


def load_users_config(env=None):
    if env is None:
        env = os.environ
    raw_users = []

    users_json = env.get("USERS_JSON")
    if users_json:
        try:
            parsed = json.loads(users_json)
            if isinstance(parsed, list):
                raw_users.extend(parsed)
        except json.JSONDecodeError:
            print("Chyba: USERS_JSON nemá platný formát JSON.")

    numbered_indices = sorted(
        int(k.split("_")[-1]) for k in env.keys() if k.startswith("BAK_USER_") and k.split("_")[-1].isdigit()
    )

    for i in numbered_indices:
        user = env.get(f"BAK_USER_{i}")
        password = env.get(f"BAK_PASS_{i}")
        if user and password:
            raw_users.append({
                "name": env.get(f"USER_NAME_{i}") or user,
                "user": user,
                "password": password,
                "current_calendar_id": env.get(f"CALENDAR_ID_{i}") or env.get(f"CURRENT_CALENDAR_ID_{i}"),
                "permanent_calendar_id": env.get(f"PERMANENT_CALENDAR_ID_{i}"),
                "archive_calendar_id": env.get(f"ARCHIVE_CALENDAR_ID_{i}") or env.get(f"HISTORY_CALENDAR_ID_{i}"),
                "bak_url": env.get(f"BAK_URL_{i}"),
            })

    legacy_user = env.get("BAK_USER")
    legacy_pass = env.get("BAK_PASS")

    if legacy_user and legacy_pass:
        if not any(u.get("user") == legacy_user for u in raw_users):
            raw_users.append({
                "name": env.get("USER_NAME") or legacy_user,
                "user": legacy_user,
                "password": legacy_pass,
                "current_calendar_id": env.get("CALENDAR_ID") or env.get("CURRENT_CALENDAR_ID"),
                "permanent_calendar_id": env.get("PERMANENT_CALENDAR_ID"),
                "archive_calendar_id": env.get("ARCHIVE_CALENDAR_ID") or env.get("HISTORY_CALENDAR_ID"),
                "bak_url": env.get("BAK_URL"),
            })

    if not raw_users:
        raise KeyError("Nebyl nalezen žádný uživatel k synchronizaci.")

    default_bak_url = env.get("BAK_URL", BAK_URL)
    return [_normalize_user_entry(u, idx + 1, default_bak_url=default_bak_url) for idx, u in enumerate(raw_users)]


def sync_single_user(user_config, creds_dict=None, room_mapping=None, service=None, today=None, env=None):
    if env is None:
        env = os.environ
    if room_mapping is None:
        room_mapping = load_room_mapping()
    if service is None:
        if creds_dict is None:
            creds_dict = json.loads(env["GOOGLE_CREDS_JSON"])
        credentials = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/calendar"],
        )
        service = build("calendar", "v3", credentials=credentials)

    user_label = user_config.get("name") or user_config["user"]
    user = user_config["user"]
    password = user_config["password"]
    current_calendar_id = user_config.get("current_calendar_id")
    permanent_calendar_id = user_config.get("permanent_calendar_id")
    archive_calendar_id = user_config.get("archive_calendar_id")
    user_bak_url = (user_config.get("bak_url") or BAK_URL).rstrip("/")
    user_base_url = f"{user_bak_url}/api/3"

    if today is None:
        today = datetime.now(ZoneInfo("Europe/Prague")).date()
        
    monday, main_end = week_window(today)
    main_start = cleanup_cutoff_date(today)

    if archive_calendar_id:
        sync_start = monday - timedelta(days=28) # 1 měsíc dozadu pro stahování z Bakalářů
        archive_cleanup_start = monday - timedelta(days=63) # 9 týdnů zpět (na promazání staré historie)
    else:
        sync_start = main_start
        
    sync_end = monday + timedelta(days=35) # 5 týdnů dopředu

    # 1. Promazání expirované historie z kalendářů
    expired_deleted = delete_expired_events(service, [current_calendar_id, permanent_calendar_id], main_start)
    if archive_calendar_id:
        expired_deleted += delete_expired_events(service, [archive_calendar_id], archive_cleanup_start)

    # 2. Stažení aktuálního stavu z Googlu do paměti
    existing_events = fetch_existing_events(
        service, 
        [current_calendar_id, permanent_calendar_id, archive_calendar_id], 
        sync_start, 
        sync_end
    )

    token = get_bakalari_token(user, password, bak_url=user_bak_url)
    headers = {"Authorization": f"Bearer {token}"}

    # 3. Stažení rozvrhu
    data = fetch_timetable_for_date(headers, sync_start.isoformat(), base_url=user_base_url)
    if data is None:
        data = requests.get(f"{user_base_url}/timetable/actual", headers=headers, timeout=30).json()

    week_cursor = sync_start + timedelta(days=7)
    while week_cursor < sync_end:
        next_data = fetch_timetable_for_date(headers, week_cursor.isoformat(), base_url=user_base_url)
        if next_data is not None:
            data = merge_timetable_data(data, next_data)
        week_cursor += timedelta(days=7)

    if env.get("RESPECT_20MIN_RULE", "").lower() == "true" and not should_sync_now(data) and env.get("FORCE_SYNC", "").lower() != "true":
        print(f"[{user_label}] Synchronizace přeskočena: není 20 minut po začátku vyučovací hodiny.")
        return {"user": user_label, "skipped": True, "inserted": 0, "updated": 0, "deleted": 0, "unchanged": 0, "expired_deleted": expired_deleted}

    # Stažení absencí
    absence_response = requests.get(f"{user_base_url}/absence/student", headers=headers, timeout=30, verify=False)
    absence_response.raise_for_status()
    abs_data = absence_response.json()
    absences = extract_absences(abs_data)

    absences_by_subject = {}
    for day_abs in abs_data.get("Absences", []):
        if not isinstance(day_abs, dict):
            continue
        date_str = str(day_abs.get("Date") or "")[:10]
        if not (sync_start.isoformat() <= date_str < sync_end.isoformat()):
            continue
        total_missed = day_abs.get("Unsolved", 0) + day_abs.get("Ok", 0) + day_abs.get("Missed", 0) + day_abs.get("School", 0)
        if total_missed > 0:
            daily_map = fetch_daily_absences_by_subject(user_base_url, headers, date_str)
            for subj_name, status in daily_map.items():
                absences_by_subject[(date_str, subj_name)] = status

    subjects = {item["Id"]: item.get("Abbrev", item.get("Name", "Hodina")) for item in data.get("Subjects", []) if item.get("Id") is not None}
    subject_names = {item["Id"]: item.get("Name", "") for item in data.get("Subjects", []) if item.get("Id") is not None}
    classes = {item["Id"]: item.get("Abbrev", item.get("Name", "")) for item in data.get("Classes", []) if item.get("Id") is not None}
    teachers = {item["Id"]: (item.get("Name") or item.get("FullName") or item.get("DisplayName") or item.get("Abbrev") or "") for item in data.get("Teachers", []) if item.get("Id") is not None}
    room_names = {item["Id"]: (item.get("Name") or item.get("Abbrev") or "") for item in data.get("Rooms", []) if item.get("Id") is not None}
    rooms = {item["Id"]: convert_room_name(item.get("Abbrev", item.get("Name", "")), room_mapping) for item in data.get("Rooms", []) if item.get("Id") is not None}
    hours = {}
    for item in data.get("Hours", []):
        hour_id = item.get("Id")
        if hour_id is None:
            continue
        hour_times = extract_hour_times(item)
        if hour_times:
            hours[hour_id] = hour_times

    if not hours:
        raise RuntimeError(f"[{user_label}] API nevrátilo správné časy vyučovacích hodin.")

    inserted = 0
    updated = 0
    unchanged = 0
    deleted = 0
    active_events_by_calendar = {}

    for day in data.get("Days", []):
        date_str = day.get("Date", "")[:10]
        if not sync_start.isoformat() <= date_str < sync_end.isoformat():
            continue
        if is_day_cancelled(day):
            continue

        atoms = filter_and_sort_day_atoms(day.get("Atoms", []), subjects, hours)
        if not atoms:
            continue

        index = 0
        while index < len(atoms):
            atom, subject, hour_info = atoms[index]
            start_time, _ = hour_info
            
            target_calendar_id = determine_target_calendar(
                date_str, current_calendar_id, permanent_calendar_id, archive_calendar_id, today, main_start, main_end
            )
            
            if _is_cancelled(atom, subject) or not target_calendar_id:
                index += 1
                continue

            title = _special_title(atom, subject)
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
            if not absence_status:
                subj_full_name = subject_names.get(first_atom.get("SubjectId"), "")
                if subj_full_name:
                    absence_status = absences_by_subject.get((date_str, subj_full_name))

            teacher_name = extract_teacher_name(first_atom, teachers)
            summary = build_event_summary(subject, room, title)
            location = build_event_location(class_name, room, room_name)
            description = build_event_description(
                teacher_name=teacher_name, change_info=change_info,
                absence_status=absence_status, room_name=room_name, room_number=room,
            )

            event_id = _event_id(date_str, start_time, subject)
            event_body = {
                "id": event_id,
                "summary": summary,
                "description": description,
                "location": location,
                "start": {"dateTime": make_rfc3339(date_str, start_time), "timeZone": "Europe/Prague"},
                "end": {"dateTime": make_rfc3339(date_str, end_time), "timeZone": "Europe/Prague"},
            }
            color_id = event_color_for(change_info, title, absence_status)
            if color_id is not None:
                event_body["colorId"] = color_id

            cal_existing = existing_events.get(target_calendar_id, {})
            existing_ev = cal_existing.get(event_id)
            
            if is_event_unchanged(existing_ev, event_body):
                unchanged += 1
                active_events_by_calendar.setdefault(target_calendar_id, set()).add(event_id)
            else:
                action, final_id = smart_upsert_event(service, target_calendar_id, event_body, existing_ev is not None)
                if action == "inserted":
                    inserted += 1
                else:
                    updated += 1
                active_events_by_calendar.setdefault(target_calendar_id, set()).add(final_id)
            
            index = next_index

    # 4. Zero API Read Cleanup - Hromadné smazání zrušených/přesunutých hodin
    # Vše, co si skript v bodě 2 stáhl do paměti a nebylo přidáno do `active_events` v bodě 3, se nyní smaže.
    for cal_id, ex_events in existing_events.items():
        active_ids = active_events_by_calendar.get(cal_id, set())
        for eid in ex_events:
            if eid not in active_ids:
                if _delete_event(service, cal_id, eid):
                    deleted += 1

    print(
        f"[{user_label}] Synch. dokončena. "
        f"Přeskočeno beze změny: {unchanged}, Nové: {inserted}, Aktualizované: {updated}, "
        f"Odstraněné v tomto okně: {deleted}, Promazané staré expirované: {expired_deleted}"
    )
    return {
        "user": user_label,
        "skipped": False,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "deleted": deleted,
        "expired_deleted": expired_deleted,
    }


def main():
    users = load_users_config()
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    room_mapping = load_room_mapping()
    today = datetime.now(ZoneInfo("Europe/Prague")).date()

    if len(users) == 1:
        sync_single_user(users[0], creds_dict=creds_dict, room_mapping=room_mapping, today=today)
        return

    max_workers = min(len(users), int(os.environ.get("MAX_SYNC_WORKERS", "5")))
    print(f"Spouštím současnou synchronizaci pro {len(users)} uživatelů...")

    errors = []
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_user = {
            executor.submit(sync_single_user, user_cfg, creds_dict, room_mapping, None, today): user_cfg
            for user_cfg in users
        }
        for future in as_completed(future_to_user):
            user_cfg = future_to_user[future]
            user_label = user_cfg.get("name") or user_cfg["user"]
            try:
                results.append(future.result())
            except Exception as exc:
                print(f"[{user_label}] Chyba při synchronizaci: {exc}")
                errors.append((user_label, exc))

    total_inserted = sum(r["inserted"] for r in results)
    total_updated = sum(r["updated"] for r in results)
    total_unchanged = sum(r["unchanged"] for r in results)
    total_deleted = sum(r["deleted"] for r in results)
    print(
        f"Souhrn synchronizace ({len(results)}/{len(users)} úspěšných) — "
        f"Přeskočeno bleskově: {total_unchanged}, Vloženo: {total_inserted}, "
        f"Aktualizováno: {total_updated}, Smazáno: {total_deleted}"
    )

    if errors:
        failed_names = ", ".join(name for name, _ in errors)
        raise RuntimeError(f"Synchronizace selhala u {len(errors)} uživatelů: {failed_names}")


if __name__ == "__main__":
    main()
