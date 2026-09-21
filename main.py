import os
import json
import hashlib
import urllib3
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import Response, HTMLResponse
from cryptography.fernet import Fernet
from icalendar import Calendar, Event, Timezone, TimezoneStandard, TimezoneDaylight

# Potlačení InsecureRequestWarning pro neověřené HTTPS požadavky
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Načtení nebo vygenerování SECRET_KEY
SECRET_KEY_ENV = os.environ.get("SECRET_KEY")
if not SECRET_KEY_ENV:
    SECRET_KEY_ENV = Fernet.generate_key().decode()
    os.environ["SECRET_KEY"] = SECRET_KEY_ENV

cipher_suite = Fernet(SECRET_KEY_ENV.encode())

app = FastAPI(title="Bakaláři iCal Sync")

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Bakaláři iCal Sync</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center p-4">
    <div class="max-w-md w-full bg-white rounded-xl shadow-md p-8">
        <h1 class="text-2xl font-bold text-gray-800 mb-6 text-center">Bakaláři iCal Feed</h1>
        
        <form id="generateForm" class="space-y-4">
            <div>
                <label for="username" class="block text-sm font-medium text-gray-700">Uživatelské jméno</label>
                <input type="text" id="username" name="username" required 
                       class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
            </div>
            
            <div>
                <label for="password" class="block text-sm font-medium text-gray-700">Heslo</label>
                <input type="password" id="password" name="password" required 
                       class="mt-1 block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 border p-2">
            </div>
            
            <button type="submit" 
                    class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500">
                Vygenerovat kalendář
            </button>
        </form>

        <div id="result" class="hidden mt-6 p-4 bg-gray-50 rounded-md border border-gray-200">
            <h2 class="text-lg font-medium text-gray-900 mb-2">Váš iCal odkaz</h2>
            <div class="flex items-center space-x-2">
                <input type="text" id="icalUrl" readonly 
                       class="block w-full rounded-md border-gray-300 bg-white shadow-sm sm:text-sm border p-2 text-gray-600 focus:outline-none">
                <button type="button" onclick="copyUrl()" 
                        class="inline-flex items-center px-3 py-2 border border-gray-300 shadow-sm text-sm leading-4 font-medium rounded-md text-gray-700 bg-white hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-blue-500">
                    Kopírovat
                </button>
            </div>
            <div class="mt-4 text-sm text-gray-600 space-y-2">
                <p><strong>Návod na přidání:</strong></p>
                <ul class="list-disc pl-5 space-y-1">
                    <li><strong>Google Kalendář:</strong> Nastavení -> Přidat kalendář -> Z adresy URL</li>
                    <li><strong>Apple Kalendář:</strong> Soubor -> Nový odběr kalendáře</li>
                </ul>
            </div>
        </div>
    </div>

    <script>
        document.getElementById('generateForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData(e.target);
            try {
                const response = await fetch('/generate', {
                    method: 'POST',
                    body: formData
                });
                if (response.ok) {
                    const data = await response.json();
                    const baseUrl = window.location.origin;
                    const urlInput = document.getElementById('icalUrl');
                    urlInput.value = `${baseUrl}/ical?token=${data.token}`;
                    document.getElementById('result').classList.remove('hidden');
                } else {
                    alert('Došlo k chybě při generování odkazu.');
                }
            } catch (error) {
                alert('Chyba komunikace se serverem.');
            }
        });

        function copyUrl() {
            const urlInput = document.getElementById('icalUrl');
            urlInput.select();
            document.execCommand('copy');
            alert('Odkaz zkopírován do schránky!');
        }
    </script>
</body>
</html>
"""

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
    if len(parts) < 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        if 0 <= hours < 24 and 0 <= minutes < 60:
            return f"{hours:02d}:{minutes:02d}"
    except ValueError:
        return None
    return None

def extract_hour_times(item):
    start = (
        item.get("StartTime")
        or item.get("BeginTime")
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

@app.get("/", response_class=HTMLResponse)
async def read_root():
    return HTML_TEMPLATE

@app.post("/generate")
async def generate_token(username: str = Form(...), password: str = Form(...)):
    data = json.dumps({"u": username, "p": password}).encode('utf-8')
    token = cipher_suite.encrypt(data).decode('utf-8')
    return {"token": token}

@app.get("/ical")
async def get_ical(token: str):
    try:
        data_bytes = cipher_suite.decrypt(token.encode('utf-8'))
        data = json.loads(data_bytes.decode('utf-8'))
        username = data.get("u")
        password = data.get("p")
    except Exception:
        raise HTTPException(status_code=401, detail="Neplatný nebo vypršelý token")
        
    # Přihlášení do Bakalářů
    login_url = "https://gyarab.bakalari.cz/api/login"
    payload = {
        "client_id": "ANDR",
        "grant_type": "password",
        "username": username,
        "password": password
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    try:
        resp = requests.post(login_url, data=payload, headers=headers, verify=False, timeout=15)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Chyba při komunikaci s API Bakalářů (login)")
        
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Přihlášení selhalo (špatné jméno nebo heslo?)")
        
    access_token = resp.json().get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="Nebyl získán přístupový token")
        
    auth_headers = {
        "Authorization": f"Bearer {access_token}"
    }

    tz_prague = ZoneInfo("Europe/Prague")
    now = datetime.now(tz_prague)
    
    # Stáhneme rozvrh pro aktuální týden a následující 3 týdny
    # Bakaláři API přijímá ?date=YYYY-MM-DD
    target_dates = []
    # Začneme pondělím aktuálního týdne - 7 dní zpět až +21 dní dopředu
    monday_current = now - timedelta(days=now.weekday())
    for week_offset in range(-1, 4):
        target_monday = monday_current + timedelta(weeks=week_offset)
        target_dates.append(target_monday.strftime("%Y-%m-%d"))

    # Generování iCal
    cal = Calendar()
    cal.add('prodid', '-//Bakalari iCal Sync//gyarab//')
    cal.add('version', '2.0')
    cal.add('x-wr-calname', 'Bakaláři Rozvrh')
    cal.add('x-wr-timezone', 'Europe/Prague')

    created_events = set()

    for date_param in target_dates:
        timetable_url = f"https://gyarab.bakalari.cz/api/3/timetable/actual?date={date_param}"
        try:
            tt_resp = requests.get(timetable_url, headers=auth_headers, verify=False, timeout=15)
            if tt_resp.status_code != 200:
                continue
            tt_data = tt_resp.json()
        except Exception:
            continue

        subjects = {}
        for s in tt_data.get("Subjects", []):
            if "Id" in s:
                subjects[str(s["Id"])] = s
                subjects[s["Id"]] = s

        rooms = {}
        for r in tt_data.get("Rooms", []):
            if "Id" in r:
                rooms[str(r["Id"])] = r
                rooms[r["Id"]] = r

        teachers = {}
        for t in tt_data.get("Teachers", []):
            if "Id" in t:
                teachers[str(t["Id"])] = t
                teachers[t["Id"]] = t

        hours = {}
        for h in tt_data.get("Hours", []):
            h_id = h.get("Id")
            if h_id is not None:
                times = extract_hour_times(h)
                if times:
                    hours[str(h_id)] = times
                    hours[h_id] = times

        for day in tt_data.get("Days", []):
            date_str = day.get("Date", "")
            if not date_str:
                continue
                
            try:
                date_only = date_str[:10] 
                date_parts = [int(p) for p in date_only.split('-')]
            except Exception:
                continue
                
            for atom in day.get("Atoms", []):
                hour_id = atom.get("HourId")
                subject_id = atom.get("SubjectId")
                room_id = atom.get("RoomId")
                teacher_id = atom.get("TeacherId")
                
                hour_info = hours.get(hour_id) or hours.get(str(hour_id))
                if not hour_info:
                    continue
                    
                start_time_str, end_time_str = hour_info
                try:
                    h_begin, m_begin = map(int, start_time_str.split(':'))
                    h_end, m_end = map(int, end_time_str.split(':'))
                    dt_start = datetime(date_parts[0], date_parts[1], date_parts[2], h_begin, m_begin, 0, tzinfo=tz_prague)
                    dt_end = datetime(date_parts[0], date_parts[1], date_parts[2], h_end, m_end, 0, tzinfo=tz_prague)
                except Exception:
                    continue

                subject = subjects.get(subject_id) or subjects.get(str(subject_id)) or {}
                subj_name = subject.get("Abbrev") or subject.get("Name") or "Hodina"
                
                room = rooms.get(room_id) or rooms.get(str(room_id)) or {}
                room_name = room.get("Abbrev") or room.get("Name") or ""
                
                teacher = teachers.get(teacher_id) or teachers.get(str(teacher_id)) or {}
                teacher_name = teacher.get("Name") or teacher.get("Abbrev") or ""

                theme = atom.get("Theme", "")
                change = atom.get("Change")
                if isinstance(change, dict):
                    change_desc = change.get("Description", "")
                    change_type = change.get("ChangeType", "")
                else:
                    change_desc = ""
                    change_type = ""
                
                summary = subj_name
                if room_name:
                    summary += f" ({room_name})"
                    
                description_lines = []
                if subj_name and subject.get("Name") and subject.get("Name") != subj_name:
                    description_lines.append(f"Předmět: {subject.get('Name')}")
                if teacher_name:
                    description_lines.append(f"Vyučující: {teacher_name}")
                if room_name:
                    description_lines.append(f"Místnost: {room_name}")
                if theme:
                    description_lines.append(f"Téma: {theme}")
                if change_desc:
                    description_lines.append(f"Změna: {change_desc}")
                elif change_type:
                    description_lines.append(f"Změna: {change_type}")
                    
                event_uid = f"{date_only}_{hour_id}_{subject_id}_{room_id}@bakalari.sync"
                if event_uid in created_events:
                    continue
                created_events.add(event_uid)

                event = Event()
                event.add('summary', summary)
                event.add('dtstart', dt_start)
                event.add('dtend', dt_end)
                event.add('dtstamp', now)
                if room_name:
                    event.add('location', room_name)
                if description_lines:
                    event.add('description', "\n".join(description_lines))
                    
                event.add('uid', event_uid)
                cal.add_component(event)
                
    return Response(
        content=cal.to_ical(),
        media_type="text/calendar",
        headers={
            "Content-Disposition": "inline; filename=rozvrh.ics"
        }
    )
