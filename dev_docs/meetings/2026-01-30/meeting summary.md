## Klíčové body
---
- Byl identifikován problém, že po instalaci se nové úpravy (např. dokumentace) nedostanou ke stávajícím uživatelům, protože skript `sync_data` synchronizuje pouze data, nikoli zbytek struktury.
- Při pokusu o rozšíření `sync_data` vznikl konflikt: přepisování souborů jako `cloud.md` by mazalo uživatelské poznámky. Současně `home` adresář na serveru nerozlišoval mezi uživateli.
- Byla implementována nová adresářová struktura oddělující serverové soubory (složka `server`) a uživatelské soubory (složka `user`), aby se zabránilo konfliktům a nechtěným přepisům. Uživatelské soubory se nyní synchronizují na server do `home` adresáře daného uživatele.
- Padák prezentoval redesignovaný dynamický dashboard a novou funkci notifikací přes Telegram. Propojení účtu vyžaduje zadání unikátního kódu vygenerovaného botem.
- Byla identifikována příčina selhání automatického spouštění reportů: funkce "cooldown period", která brání opakovanému odeslání stejné notifikace v krátkém čase.
- Řešilo se fungování virtuálních prostředí (`venv`) a byla nalezena chyba ve skriptu `init` (chybějící tečka před `venv`).
- Pro ochranu serverových souborů bylo navrženo použít oprávnění `read-only`. Byl představen soubor `cloud.local.md` pro uživatelské poznámky, který se nepřepisuje.
- Synchronizační skript byl upraven přidáním parametru `--checksum`, aby se přenášely pouze soubory se změněným obsahem, nikoli jen s novějším datem modifikace.
- Byl představen plán na přesun `/home` adresářů na samostatný, zálohovaný diskový oddíl kvůli nedostatku místa na systémovém disku.
- Diskutovalo se o nefunkčním skriptu `check_freshness`, který měl kontrolovat aktuálnost dat. Padák navrhl jeho odstranění, protože `rsync` je pro tento účel dostatečně efektivní.
- Bylo zjištěno, že API pro stahování dat z Kebooly nepodporuje efektivní filtrování podle časové značky změny, což komplikuje inkrementální stahování pouze změněných řádků.
- Bylo zjištěno, že pro inkrementální synchronizaci dat z Kebooly je správným přístupem použít parametr `changed_since` v API volání namísto zastaralého a komplikovaného `where` filtru.
- Padák navrhl provádět aktualizace dat z Kebooly každých 15 minut a pro zajištění smazání starých souborů na straně klienta použít parametr `--delete` při synchronizaci.
- Byl navržen přesun výpočtů a zpracování dat (např. Infrastructure Cost Data) z drahých cloudových služeb (Snowflake) na stranu klienta pomocí DuckDB, aby se snížily náklady a zvýšila rychlost.
- Pro nová prostředí bude v `settings.json` nastaveno, aby systém vyžadoval potvrzení pro rizikové operace (např. `Push Force`).
- Padák experimentuje s vývojem desktopové aplikace pro macOS pro zobrazování notifikací.
## Přijatá rozhodnutí
---
- Skript `sync_data` bude synchronizovat veškerý potřebný obsah ze serveru (skripty, dokumentaci atd.), nejen data.
- Byla zavedena nová adresářová struktura striktně oddělující serverové (`server`) a uživatelské (`user`) soubory.
- Pro notifikace bude v pilotní fázi použit Telegram s ověřováním přes unikátní kód.
- Byl implementován volitelný mechanismus "cooldown period" pro zamezení zahlcení uživatelů notifikacemi.
- Ochrana serverových souborů bude řešena instrukcemi `read-only` v `Cloud.md` a využitím souboru `cloud.local.md` pro uživatelské poznámky.
- Pro synchronizaci dat se bude používat parametr `--checksum`, aby se předešlo zbytečným přenosům dat.
- Skript pro kontrolu aktuálnosti dat (`check_freshness`) bude odstraněn a pro synchronizaci se bude spoléhat výhradně na `rsync`.
- Pro inkrementální export dat z Kebuly se bude používat parametr `changed_since` místo `where` filtrů.
- Aktualizace dat z Kebuly se budou provádět v 15minutových intervalech.
- Pro synchronizaci souborů se bude používat parametr `--delete` k zajištění smazání souborů, které již neexistují na zdroji.
- Zpracování dat se bude přesouvat z cloudových služeb (Snowflake) na stranu klienta s využitím DuckDB.
- Přetypování dat (kastování) se nebude provádět na straně klienta, aby byla zachována kvalita zdrojových parquet souborů.
## Akční body
---
### Úkoly
| Task | Responsible Party | Deadline | Notes |
| :--- | :--- | :--- | :--- |
| Doplnit do `cloud.md` instrukce pro uživatele ohledně ukládání do složky `artifacts`. | Matěj Kis (předpoklad) / Tým | Není specifikováno | Potřeba aktualizace byla zmíněna, ale obsah nebyl aktivně řešen. |
| Připojit se ke Claude a komunikovat s ním. | Padák | Není specifikováno | Zmíněno jako další krok na konci schůzky. |
| Zdebugovat, proč se automatická notifikace v 7:30 nespustila dle plánu v `crontab`. | Padák | Není specifikováno | Příčinou je pravděpodobně funkce "cooldown period". |
| Doplnit informace o `venv` do dokumentace. | Padák | Není specifikováno | Doplnit popis do `Docs/Notifications.md`. |
| Opravit chybu ve skriptu `init` (přidat tečku před `venv`). | Speaker 2 | Není specifikováno | Zajistit správné vytváření virtuálního prostředí. |
| Zkontrolovat, co se reálně děje při aktualizaci dat z Kebooly. | Speaker 2 | Není specifikováno | V návaznosti na změnu synchronizace pomocí `--checksum`. |
| Upravit synchronizační skript, aby synchronizoval i soubor `cloud.local.md` ze stroje uživatele na server. | Speaker 2 | Není specifikováno | Zajistí zálohu uživatelských poznámek na serveru. |
| Předělat strukturu serveru, přesunout uživatelské home adresáře na samostatný zálohovaný disk. | Padák | Není specifikováno | V rámci research úkolu na "Backup and disaster recovery". |
| Opravit problém s oprávněními souborového systému u Telegram bota. | Padák | Není specifikováno | Zapsáno v GIDA píšu jako poznámka k úpravě tlačítek v Telegramu. |
| Zkontrolovat a případně doplnit skripty pro nasazení (deploy scripts) o správné nastavení oprávnění složek. | Speaker 2 | Není specifikováno | Zajistit, aby skripty automatizovaly ruční úpravy oprávnění. |
| Zkontrolovat, proč se nestahují metadata (soubor .metadata). | Speaker 2 | Není specifikováno | Původní problém byl v chybějících oprávněních k zápisu. |
| Zjistit, kde se aplikují `where` filtry definované v `Data Description`. | Speaker 2 | Není specifikováno | Zvláštní pozornost věnovat použití sloupce s časovým razítkem (timestamp). |
| Upravit exportní skript tak, aby pro inkrementální synchronizaci používal parametr `changed_since`. | Speaker 2 | Není specifikováno | Cílem je zjednodušit logiku a zajistit efektivní načítání pouze změněných dat. |
| Připravit a promyslet způsob zacházení s daty v tabulkách (např. při přemazání). | Speaker 2 | Není specifikováno | Bude řešeno v rámci celé flow inkrementálního zpracování. |
| Vyzkoušet funkci `AirSync` a zaměřit se na její chování při synchronizaci. | Speaker 2 | Není specifikováno | Zapsáno k řešení v momentě implementace. |
| Zkontrolovat telemetrická data a připravit je k synchronizaci. | Speaker 2 | Dnes | Ověřit správnost dat před jejich nasynchronizováním. |
| Práce na implementaci (v branchi) s možností review přes pull request. | Speaker 2 | Není specifikováno | Padák nabídl provedení revize kódu. |
| Vložit obsah `settings.json` do nového GitHub issue a přiřadit ho Daše Dama. | Padák | Není specifikováno | Cílem je, aby Daša Dama toto nastavení zapracovala do init skriptu. |
| Dokončit úpravy notifikací, přidat podporu pro Slack a opravit chyby v ručním spouštění reportů. | Padák | Není specifikováno | |
| Vyvinout a otestovat prototyp desktopové aplikace pro macOS pro zobrazování notifikací. | Padák | Není specifikováno | Probíhá v rámci větve `MacOS app branch`. |
| Provést review query pro data o nákladech. | Matěj | Není specifikováno | Bude řešeno na začátku příštího týdne. |
| Zkontrolovat, zda se skripty pro synchronizaci (tablety) spouštějí správně. | Speaker 3 | Není specifikováno | Pokud ne, odstranit `check sam` ze `Sync scriptu`. Nízká priorita. |
| Podívat se na query týkající se nákladů (costů). | Speaker 2 | Není specifikováno | |
| Řešit "certifikované query". | Matěj | Příští týden | Cílem je vytvořit "top odpovídačku". |
### Termíny
- **za 10 dní**: Vytvoření nových PSUGO projektů (dle příkladu notifikačního skriptu, nejedná se o skutečný termín).
- **7:30 (evropského času)**: Měla proběhnout automatická notifikace pro uživatele Petr, což se nestalo.
- **Dnes**: Speaker 2 se bude věnovat kontrole telemetrických dat a jejich přípravě k synchronizaci.
- **Příští týden**: Matěj bude řešit certifikované query.
### Následné kroky
- Pokračovat v diskuzi a spolupráci s AI (Claude).
- Padák prověří a opraví problém s automatickým spouštěním reportů naplánovaných přes `crontab`.
- Padák připraví a provede přesun uživatelských `home` adresářů na nový diskový oddíl.
- Speaker 2 se podrobněji podívá na proces inicializace virtuálních prostředí a synchronizace dat, včetně implementace zálohování `cloud.local.md`.
- Zvážit odstranění skriptu `check_freshness` a spoléhat se výhradně na `rsync`.
- Prozkoumat možnosti API pro stahování dat, zda by bylo možné efektivněji filtrovat pouze změněné záznamy.
- Speaker 2 provede revizi a úpravu skriptu pro export dat z Kebuly s využitím parametru `changed_since`.
- Speaker 2 se bude zabývat daty, jejich strukturou a celkovým procesem synchronizace, Padák bude k dispozici pro revizi kódu.
- Daša Dama implementuje nová nastavení z `settings.json` do init skriptu prostředí.
- Padák bude pokračovat ve vývoji desktopové aplikace pro notifikace.