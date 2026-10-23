import os
import json
import requests
from datetime import datetime
import sync

def main():
    print("Načítám konfiguraci...")
    users = sync.load_users_config()
    if not users:
        print("Nenalezen žádný uživatel. Zkontroluj proměnné prostředí.")
        return

    # Pro test si vezmeme prvního uživatele
    user = users[0] 
    bak_url = (user.get("bak_url") or sync.BAK_URL).rstrip("/")
    base_url = f"{bak_url}/api/3"
    
    print(f"Přihlašuji uživatele {user.get('name', user['user'])}...")
    token = sync.get_bakalari_token(user["user"], user["password"], bak_url)
    headers = {"Authorization": f"Bearer {token}"}
    
    today = datetime.now().strftime("%Y-%m-%d")
    
    print(f"Stahuji rozvrh pro {today}...")
    tt_resp = requests.get(f"{base_url}/timetable/actual?date={today}", headers=headers, verify=False)
    
    print("Stahuji modul absencí...")
    abs_resp = requests.get(f"{base_url}/absence/student", headers=headers, verify=False)
    
    data = {
        "info": "Tento soubor obsahuje surová data z Bakalářů pro analýzu absencí.",
        "timetable_response": tt_resp.json() if tt_resp.ok else {"error": tt_resp.text},
        "absence_response": abs_resp.json() if abs_resp.ok else {"error": abs_resp.text}
    }
    
    with open("bakalari_dump.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        
    print("--------------------------------------------------")
    print("HOTOVO! Data byla uložena do souboru 'bakalari_dump.json'.")
    print("Podívej se do tohoto souboru (např. pomocí vyhledávání Ctrl+F a zadej 'Němčina' nebo čas hodiny).")
    print("Zjistíme tak, pod jakým klíčem Bakaláři schovávají dnešní nezpracovanou absenci.")

if __name__ == "__main__":
    main()
