import os
import json
import requests
from datetime import datetime
import sync

def main():
    print("Načítám konfiguraci pro hledání skrytých dat...")
    users = sync.load_users_config()
    if not users:
        print("Nenalezen žádný uživatel.")
        return

    user = users[0] 
    bak_url = (user.get("bak_url") or sync.BAK_URL).rstrip("/")
    base_url = f"{bak_url}/api/3"
    
    print(f"Přihlašuji uživatele {user.get('name', user['user'])}...")
    token = sync.get_bakalari_token(user["user"], user["password"], bak_url)
    headers = {"Authorization": f"Bearer {token}"}
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    data = {
        "info": "Rozšířený dump pro odhalení skrytých detailů o absenci.",
        "pokusy_o_detail": {}
    }
    
    # Testujeme endpointy, kde by se mohl skrývat detail absencí
    test_endpoints = [
        f"{base_url}/absence/student",
        f"{base_url}/absence/student?from={today}&to={today}",
        f"{base_url}/absence/student/detail",
        f"{base_url}/attendance/student",
        f"{base_url}/classregister/student",
        f"{base_url}/timetable/actual?date={today}&includeAbsence=true"
    ]
    
    for url in test_endpoints:
        print(f"Zkouším tajný endpoint: {url}")
        resp = requests.get(url, headers=headers, verify=False)
        try:
            data["pokusy_o_detail"][url] = resp.json() if resp.ok else {"status": resp.status_code, "error": resp.text}
        except:
            data["pokusy_o_detail"][url] = "Chyba parsování (asi neplatný endpoint)"

    with open("bakalari_dump.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        
    print("HOTOVO! Podívej se do bakalari_dump.json na sekci 'pokusy_o_detail'.")

if __name__ == "__main__":
    main()
