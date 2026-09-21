import os
import json
import hashlib
import requests
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build

BASE_URL = "https://gyarab.bakalari.cz/api/3"
LOGIN_URL = "https://gyarab.bakalari.cz/api/login"

def get_bakalari_token(user, password):
    res = requests.post(
        LOGIN_URL,
        data={
            "client_id": "ANDR",
            "grant_type": "password",
            "username": user,
            "password": password
        },
        timeout=15
    )
    res.raise_for_status()
    return res.json()["access_token"]

def main():
    user = os.environ["BAK_USER"]
    password = os.environ["BAK_PASS"]
    calendar_id = os.environ["CALENDAR_ID"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])

    # Google Calendar service
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    service = build("calendar", "v3", credentials=credentials)

    # Bakalari data
    token = get_bakalari_token(user, password)
    headers = {"Authorization": f"Bearer {token}"}
    
    # Stáhneme aktuální rozvrh na tento týden
    r = requests.get(f"{BASE_URL}/timetable/actual", headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()

    # Mapování ID na názvy
    subjects = {s["Id"]: s["Abbrev"] for s in data.get("Subjects", [])}
    rooms = {r["Id"]: r["Abbrev"] for r in data.get("Rooms", [])}
    hours = {h["Id"]: (h["StartTime"], h["EndTime"]) for h in data.get("Hours", [])}

    for day in data.get("Days", []):
        date_str = day["Date"][:10]  # YYYY-MM-DD
        for atom in day.get("Atoms", []):
            subj = subjects.get(atom.get("SubjectId"), "Hodina")
            room = rooms.get(atom.get("RoomId"), "")
            hour_info = hours.get(atom.get("HourId"))
            
            if not hour_info:
                continue

            start_iso = f"{date_str}T{hour_info[0]}:00"
            end_iso = f"{date_str}T{hour_info[1]}:00"
            change_info = atom.get("Change", {}).get("Description", "")

            # Unikátní ID pro Google Calendar (jen malá písmena a čísla)
            raw_id = f"{date_str}_{atom.get('HourId')}_{subj}"
            event_id = hashlib.md5(raw_id.encode()).hexdigest()

            event_body = {
                "id": event_id,
                "summary": f"{subj} ({room})" if room else subj,
                "description": f"Změna/Suplování: {change_info}" if change_info else "Běžná hodina",
                "location": room,
                "start": {"dateTime": start_iso, "timeZone": "Europe/Prague"},
                "end": {"dateTime": end_iso, "timeZone": "Europe/Prague"}
            }

            try:
                service.events().insert(calendarId=calendar_id, body=event_body).execute()
            except Exception:
                # Pokud událost existuje, zaktualizujeme ji
                service.events().update(calendarId=calendar_id, eventId=event_id, body=event_body).execute()

    print("Synchronizace úspěšně dokončena.")

if __name__ == "__main__":
    main()
