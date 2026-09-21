import os
import json
import hashlib
import requests

from google.oauth2 import service_account
from googleapiclient.discovery import build


BAK_URL = os.getenv(
    "BAK_URL",
    "https://gyarab.bakalari.cz",
).rstrip("/")

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
        raise RuntimeError(
            f"Bakaláři nevrátil access_token. Odpověď: {data}"
        )

    return data["access_token"]


def main():
    user = os.environ["BAK_USER"]
    password = os.environ["BAK_PASS"]
    calendar_id = os.environ["CALENDAR_ID"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])

    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )

    service = build(
        "calendar",
        "v3",
        credentials=credentials,
    )

    token = get_bakalari_token(user, password)
    headers = {
        "Authorization": f"Bearer {token}",
    }

    response = requests.get(
        f"{BASE_URL}/timetable/actual",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()

    subjects = {
        item["Id"]: item["Abbrev"]
        for item in data.get("Subjects", [])
    }

    rooms = {
        item["Id"]: item["Abbrev"]
        for item in data.get("Rooms", [])
    }

    hours = {
        item["Id"]: (
            item["StartTime"],
            item["EndTime"],
        )
        for item in data.get("Hours", [])
    }

    inserted = 0
    updated = 0

    for day in data.get("Days", []):
        date_str = day["Date"][:10]

        for atom in day.get("Atoms", []):
            subject = subjects.get(
                atom.get("SubjectId"),
                "Hodina",
            )

            room = rooms.get(
                atom.get("RoomId"),
                "",
            )

            hour_info = hours.get(atom.get("HourId"))

            if not hour_info:
                continue

            start_time, end_time = hour_info

            start_iso = f"{date_str}T{start_time}:00"
            end_iso = f"{date_str}T{end_time}:00"

            change_info = atom.get(
                "Change",
                {},
            ).get(
                "Description",
                "",
            )

            raw_id = (
                f"{date_str}_"
                f"{atom.get('HourId')}_"
                f"{subject}"
            )

            event_id = hashlib.md5(
                raw_id.encode("utf-8")
            ).hexdigest()

            event_body = {
                "id": event_id,
                "summary": (
                    f"{subject} ({room})"
                    if room
                    else subject
                ),
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

            except Exception as error:
                # Událost už existuje, proto ji aktualizujeme.
                if "already exists" not in str(error).lower():
                    try:
                        service.events().get(
                            calendarId=calendar_id,
                            eventId=event_id,
                        ).execute()
                    except Exception:
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
        f"aktualizované události: {updated}"
    )


if __name__ == "__main__":
    main()
