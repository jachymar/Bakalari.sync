import os
import json
import urllib3
import requests
from datetime import datetime
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import Response, HTMLResponse
from cryptography.fernet import Fernet
from icalendar import Calendar, Event

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
        resp = requests.post(login_url, data=payload, headers=headers, verify=False, timeout=10)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Chyba při komunikaci s API Bakalářů (login)")
        
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Přihlášení selhalo (špatné jméno nebo heslo?)")
        
    access_token = resp.json().get("access_token")
    if not access_token:
        raise HTTPException(status_code=401, detail="Nebyl získán přístupový token")
        
    # Získání aktuálního rozvrhu
    timetable_url = "https://gyarab.bakalari.cz/api/3/timetable/actual"
    auth_headers = {
        "Authorization": f"Bearer {access_token}"
    }
    
    try:
        tt_resp = requests.get(timetable_url, headers=auth_headers, verify=False, timeout=10)
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Chyba při stahování rozvrhu")
        
    if tt_resp.status_code != 200:
        raise HTTPException(status_code=500, detail="Selhalo načtení rozvrhu z API")
        
    tt_data = tt_resp.json()
    
    # Generování iCal
    cal = Calendar()
    cal.add('prodid', '-//Bakalari iCal Sync//gyarab//')
    cal.add('version', '2.0')
    
    subjects = {s["Id"]: s for s in tt_data.get("Subjects", [])}
    rooms = {r["Id"]: r for r in tt_data.get("Rooms", [])}
    hours = {h["Id"]: h for h in tt_data.get("Hours", [])}
    
    for day in tt_data.get("Days", []):
        date_str = day.get("Date", "")
        if not date_str:
            continue
            
        try:
            date_only = date_str[:10] 
            date_obj = datetime.strptime(date_only, "%Y-%m-%d")
        except ValueError:
            continue
            
        for atom in day.get("Atoms", []):
            hour_id = atom.get("HourId")
            subject_id = atom.get("SubjectId")
            room_id = atom.get("RoomId")
            
            if not hour_id or not subject_id:
                continue
                
            hour_info = hours.get(hour_id)
            if not hour_info:
                continue
                
            begin_time_str = hour_info.get("BeginTime")
            end_time_str = hour_info.get("EndTime")
            
            if not begin_time_str or not end_time_str:
                continue
                
            try:
                h_begin, m_begin = map(int, begin_time_str.split(':'))
                h_end, m_end = map(int, end_time_str.split(':'))
            except ValueError:
                continue
            
            dt_start = date_obj.replace(hour=h_begin, minute=m_begin, second=0)
            dt_end = date_obj.replace(hour=h_end, minute=m_end, second=0)
            
            subject = subjects.get(subject_id, {})
            subj_abbrev = subject.get("Abbrev", "Neznámý")
            
            room = rooms.get(room_id, {})
            room_abbrev = room.get("Abbrev", "")
            
            theme = atom.get("Theme", "")
            change = atom.get("Change")
            
            summary = subj_abbrev
            if room_abbrev:
                summary += f" ({room_abbrev})"
                
            description = ""
            if theme:
                description += f"Téma: {theme}\n"
            if change:
                change_desc = change.get("Description", "")
                if change_desc:
                    description += f"Změna: {change_desc}\n"
                
            event = Event()
            event.add('summary', summary)
            event.add('dtstart', dt_start)
            event.add('dtend', dt_end)
            if description:
                event.add('description', description.strip())
                
            uid = f"{date_only}-{hour_id}-{subject_id}@bakalari.sync"
            event.add('uid', uid)
            
            cal.add_component(event)
            
    return Response(content=cal.to_ical(), media_type="text/calendar")
