import os
import json
import hashlib
import requests

from datetime import datetime
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


BAK_URL = os.getenv("BAK_URL", "https://gyarab.bakalari.cz").rstrip("/")
BASE_URL = f"{BAK_URL}/api/3"


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

    if ":" in text:
        parts = text.split(":")
        if len(parts) >= 2:
            hours = parts[0].zfill(2)
            minutes = parts[1].zfill(2)
            return f"{hours}:{minutes}"

    return text


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
    """
    Převádí datum a čas do RFC3339 formátu s časovou zónou Europe/Prague.
    Příklad: 2026-09-21T07:10:00+02:00
    """
    time_str = str(time_str).strip()
    if len(time_str.split(":")) == 2:
        time_str = f"{time_str}:00"

    local_dt = datetime.fromisoformat(f"{date_str}T{time_str}").replace(
        tzinfo=ZoneInfo("Europe/Prague")
    )
    return local_dt.isoformat()


def main():
    user = os.environ["BAK_USER"]
    password = os.environ["BAK_PASS"]
    calendar_id = os.environ["CALENDAR_ID"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])

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

    subjects = {
        item["Id"]: item.get("Abbrev", item.get("Name", "Hodina"))
        for item in data.get("Subjects", [])
        if item.get("Id") is not None
    }

    rooms = {
        item["Id"]: item.get("Abbrev", item.get("Name", ""))
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

    for day in data.get("Days", []):
        date_str = day["Date"][:10]

        for atom in day.get("Atoms", []):
            subject = subjects.get(atom.get("SubjectId"), "Hodina")
            room = rooms.get(atom.get("RoomId"), "")
            hour_id = atom.get("HourId")
            hour_info = hours.get(hour_id)

            if not hour_info:
                continue

            start_time, end_time = hour_info
            start_iso = make_rfc3339(date_str, start_time)
            end_iso = make_rfc3339(date_str, end_time)

            change_info = (
                atom.get("Change", {}).get("Description", "")
                if isinstance(atom.get("Change"), dict)
                else ""
            )

            raw_id = f"{date_str}_{hour_id}_{subject}"
            event_id = hashlib.md5(raw_id.encode("utf-8")).hexdigest()

            event_body = {
                "id": event_id,
                "summary": f"{subject} ({room})" if room else subject,
                "description": (
                    f"Změna/Suplování: {change_info}"
                    if change_info
                    else "Běžná hodina"
                ),
                "location": room,
                "start": {
                    "dateTime": start_iso,
                    "timeZone": "Europe/Prague",
                },
                "end": {
                    "dateTime": end_iso,
                    "timeZone": "Europe/Prague",
                },
            }

            try:
                service.events().insert(
                    calendarId=calendar_id,
                    body=event_body,
                ).execute()
                inserted += 1

            except HttpError as error:
                # 409 = už existuje, pak aktualizujeme
                if error.resp.status != 409:
                    raise

                service.events().update(
                    calendarId=calendar_id,
                    eventId=event_id,
                    body=event_body,
                ).execute()
                updated += 1

    print(
        "Synchronizace úspěšně dokončena. "
        f"Nové události: {inserted}, "
        f"Aktualizované události: {updated}"
    )


if __name__ == "__main__":
    main()
