# Bakalari.sync

Automatická synchronizace rozvrhu a absencí z Bakalářů do Google Kalendáře. Podporuje jak jednoho uživatele, tak **současnou (paralelní) synchronizaci více lidí najednou**.

---

## Možnosti konfigurace uživatelů

Skript načítá uživatele v následujícím pořadí:

### 1. Pomocí `USERS_JSON` (doporučeno pro více lidí)
Nastavte proměnnou prostředí (nebo GitHub Secret) `USERS_JSON` jako JSON pole se seznamem uživatelů. Všichni uživatelé se budou synchronizovat **současně (paralelně)**:

```json
[
  {
    "name": "Adam",
    "user": "adam.novak",
    "password": "heslo1",
    "current_calendar_id": "id_kalendare_1@group.calendar.google.com",
    "permanent_calendar_id": "id_kalendare_1_perm@group.calendar.google.com"
  },
  {
    "name": "Eva",
    "user": "eva.svobodova",
    "password": "heslo2",
    "calendar_id": "id_kalendare_2@group.calendar.google.com"
  }
]
```

*(Volitelně lze u každého uživatele uvést i vlastní `"bak_url"`, pokud chodí na jinou školu).*

### 2. Pomocí číslovaných proměnných (`_1`, `_2`, ...)
Pokud nechcete psát JSON, můžete přidat očíslované proměnné prostředí pro libovolný počet lidí:

- `BAK_USER_1`, `BAK_PASS_1`, `CALENDAR_ID_1` (nebo `CURRENT_CALENDAR_ID_1` a `PERMANENT_CALENDAR_ID_1`), volitelně `USER_NAME_1`
- `BAK_USER_2`, `BAK_PASS_2`, `CALENDAR_ID_2` (nebo `CURRENT_CALENDAR_ID_2` a `PERMANENT_CALENDAR_ID_2`), volitelně `USER_NAME_2`
- atd.

### 3. Původní konfigurace pro 1 uživatele (zpětně kompatibilní)
- `BAK_USER`
- `BAK_PASS`
- `CALENDAR_ID` (nebo `CURRENT_CALENDAR_ID` a `PERMANENT_CALENDAR_ID`)

---

## Společné proměnné prostředí

- `GOOGLE_CREDS_JSON` – JSON klíč Google Service Accountu (nezapomeňte nasdílet kalendáře všech uživatelů s e-mailem tohoto Service Accountu s oprávněním **Provádět změny událostí**).
- `BAK_URL` – výchozí URL Bakalářů (výchozí `https://gyarab.bakalari.cz`).
- `MAX_SYNC_WORKERS` – maximální počet souběžných vláken při synchronizaci více lidí (výchozí `5`).
