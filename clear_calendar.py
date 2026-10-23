import os
import json
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
import sync

def main():
    print("Načítám uživatele a připojuji se k API přes existující servisní účet (bez nutnosti přihlášení)...")
    users = sync.load_users_config()
    
    creds_dict = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    credentials = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    service = build("calendar", "v3", credentials=credentials)
    
    total_deleted = 0

    for user in users:
        name = user.get("name", user["user"])
        # Získáme unikátní kalendáře daného uživatele (aktuální i permanentní)
        cals = set(c for c in [user.get("current_calendar_id"), user.get("permanent_calendar_id")] if c)
        
        for cal_id in cals:
            print(f"[{name}] Mažu vygenerované události v kalendáři: {cal_id} ...")
            page_token = None
            deleted_in_cal = 0
            
            while True:
                response = sync.execute_with_backoff(
                    service.events().list(
                        calendarId=cal_id,
                        singleEvents=True,
                        maxResults=250,
                        pageToken=page_token
                    )
                )
                
                for event in response.get("items", []):
                    event_id = event.get("id", "")
                    
                    # Náš synchronizační skript generuje ID striktně jako 32znakový MD5 hash
                    # (s případným sufixem _restored). Mažeme POUZE takové události.
                    if re.fullmatch(r"[0-9a-f]{32}(_restored)?", event_id):
                        try:
                            sync._delete_event(service, cal_id, event_id)
                            deleted_in_cal += 1
                        except Exception as e:
                            print(f"Chyba při mazání {event_id}: {e}")
                            
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
                    
            print(f"[{name}] Smazáno {deleted_in_cal} událostí v tomto kalendáři.")
            total_deleted += deleted_in_cal
    
    print("--------------------------------------------------")
    print(f"HOTOVO! Celkově bylo úspěšně smazáno {total_deleted} událostí.")
    print("Nyní je kalendář čistý a můžeš spustit běžnou synchronizaci.")

if __name__ == "__main__":
    main()
