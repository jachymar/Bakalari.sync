# 📅 Bakalari.sync

Automatická synchronizace rozvrhu, školních akcí a absencí z Bakalářů do Google Kalendáře. Podporuje jak jednoho uživatele, tak **současnou (paralelní) synchronizaci více lidí najednou**.

---

## 🌟 Klíčové funkce a mechaniky

### 🗓️ 1. Hlavní kalendář (Přehledné aktivní okno)
Udržuje pouze aktuální rozvrh, aby nebyl kalendář zbytečně přeplněný starými daty:
*   **Pondělí – Čtvrtek:** Zobrazuje **aktuální týden + příští týden** (a ponechává ještě minulý týden pro rychlou kontrolu).
*   **Pátek – Neděle:** Automaticky vymaže minulý týden a vygeneruje rozvrh na **2 týdny dopředu** (aktuální + příští + přespříští týden).

### 🗄️ 2. Archivní / Historický kalendář (Bez duplicit!)
Pokud se potřebuješ podívat do historie (např. absence před měsícem) nebo na vzdálenější výhled, můžeš přidat druhý kalendář (`archive_calendar_id`):
*   **Jak to funguje:** V aplikaci Google Kalendář ho máš běžně vypnutý (odškrtnutý). Zapneš ho jen, když potřebuješ.
*   **Rozsah:** Generuje rozvrh **9 týdnů (cca 2 měsíce) dozadu** a **5 týdnů (cca 1 měsíc) dopředu**.
*   **Žádné duplicity:** Archivní kalendář *automaticky vynechává* týdny, které aktuálně svítí v hlavním kalendáři. Jakmile v pátek hlavní kalendář smaže minulý týden, přesune se automaticky do archivu. V mobilu tak na sebe oba kalendáře dokonale navazují.

### 🔗 3. Inteligentní spojování akcí
*   Vícehodinové akce (`Exkurze`, `Projekt`, `Výlet`) se automaticky spojují do jednoho souvislého bloku přes přestávky.
*   **Ochrana proti dírám:** Pokud je uprostřed celodenní exkurze běžná hodina v Bakalářích označena jako zrušená (`Absence of class`), skript ji inteligentně přemostí a exkurzi nerozdělí.

### 🎨 4. Barvy a absence
*   ⚪ **Šedá (Graphite):** Běžné a suplované hodiny.
*   🟢 **Zelená (Sage):** Celotřídní akce a exkurze.
*   🔵 **Modrozelená (Peacock):** Školní absence (reprezentace školy, soutěž).
*   🔴 **Červená/Růžová (Flamingo):** Omluvená nebo neomluvená absence (v popisu události je uveden přesný stav).

---

## ⚙️ Možnosti konfigurace uživatelů

Skript umožňuje nastavit uživatele třemi způsoby (načítají se v tomto pořadí):

### Varianta A: Pomocí `USERS_JSON` *(Doporučeno pro více lidí)*
Nastavte GitHub Secret `USERS_JSON` jako JSON pole se seznamem uživatelů. Všichni se budou synchronizovat **současně**.

```json
[
  {
    "name": "Adam",
    "user": "adam.novak",
    "password": "heslo1",
    "current_calendar_id": "id_kalendare_1@group.calendar.google.com",
    "permanent_calendar_id": "id_kalendare_1_perm@group.calendar.google.com",
    "archive_calendar_id": "id_kalendare_1_archiv@group.calendar.google.com"
  },
  {
    "name": "Eva",
    "user": "eva.svobodova",
    "password": "heslo2",
    "calendar_id": "id_kalendare_2@group.calendar.google.com",
    "archive_calendar_id": "id_kalendare_2_archiv@group.calendar.google.com"
  }
]
```
> 💡 *Tip: Pole `permanent_calendar_id` a `archive_calendar_id` jsou volitelná. Volitelně lze uvést i vlastní `bak_url`.*

### Varianta B: Pomocí číslovaných proměnných (`_1`, `_2`, ...)
Pokud nechcete formátovat JSON, přidejte pro každého uživatele očíslované proměnné (Secrets):
*   `BAK_USER_1`, `BAK_PASS_1`
*   `CALENDAR_ID_1` (nebo `CURRENT_CALENDAR_ID_1` a `PERMANENT_CALENDAR_ID_1`)
*   *Volitelně:* `ARCHIVE_CALENDAR_ID_1` (nebo `HISTORY_CALENDAR_ID_1`) pro archiv.
*   *Volitelně:* `USER_NAME_1` pro hezčí výpisy v logu.
*   Pro další uživatele pokračujte `_2`, `_3` atd.

### Varianta C: Původní konfigurace (1 uživatel)
Zpětně kompatibilní nastavení pro jednoho studenta:
*   `BAK_USER`, `BAK_PASS`
*   `CALENDAR_ID` (nebo `CURRENT_CALENDAR_ID` a `PERMANENT_CALENDAR_ID`)
*   *Volitelně:* `ARCHIVE_CALENDAR_ID` (nebo `HISTORY_CALENDAR_ID`)

---

## 🔐 Společné proměnné prostředí

Tato nastavení platí pro celý skript bez ohledu na počet uživatelů:

*   🔑 **`GOOGLE_CREDS_JSON` (Povinné)** – JSON klíč Google Service Accountu. Nezapomeňte s e-mailem tohoto účtu nasdílet všechny kalendáře s oprávněním *Provádět změny událostí*.
*   🔗 **`BAK_URL`** – Výchozí adresa školy (výchozí: `https://gyarab.bakalari.cz`).
*   🚀 **`MAX_SYNC_WORKERS`** – Kolik uživatelů se má synchronizovat najednou (výchozí: `5`).
