## Informace o schůzce
> Datum: 2026-02-01 20:35:35
> Místo: [Vložit místo]
> Účastníci: [Padák] [Speaker 2] [Speaker 3] [Speaker 4]
## Poznámky ze schůzky
- Synchronizace a distribuce skriptů, dokumentace a dat (CloudMD, Sync/AirSync, rsync)
  - Aktuální sync řeší pouze data (parquet), nikoli skripty ani dokumentaci; CloudMD (dříve project.json) se u uživatele neaktualizuje.
  - Dočasně se používá ruční rsync; návrh rozšířit „sync data“ na synchronizaci všeho ze serveru (data, skripty, dokumentace) a zavést obousměrnost pro User foldery.
  - Identifikován problém s přepisováním uživatelských úprav CloudMD při každém stažení; potřeba strategie pro merge/ochranu změn (lokální overrides, verzování).
  - Hrozba nechtěné distribuce uživatelských skriptů všem; chybí identifikace vlastníka (User/scripts bez namespace).
  - AI zasahovala do serverové složky Scripts; nutná ochrana/locky a jasná práva, aby nedocházelo k přepisům.
  - Struktura adresářů: lokálně CloudMD + serverové složky (Docs, Example, Metadata, Parked, Scripts) a User (Artifacts, DuckDB, Notifications, Parked, Scripts). Na serveru parita server folderu a Users v home.
  - SyncData nyní synchronizuje User folder na server a Server folder ze serveru; zpětná synchronizace do lokálního User zatím není.
  - Přidán rsync parametr --checksum kvůli častým změnám mtime u parquetů; snižuje zbytečné přenosy, ale zvyšuje IO/CPU.
  - AirSync porovnává velikost/timestamp a rychle končí při nezměněných souborech; doporučeno nahradit „check freshness“ AirSyncem a správně logovat metadatové soubory (.metadata/JSON).
  - Potřeba parametru „--delete“ (force) v AirSync pro odstranění přebytečných lokálních souborů a zrcadlení stavu serveru.
- Inkrementální export z Kebuly, granularita a API omezení
  - Rozdíl full export vs. inkrementální export dle timestampu; potvrzeno použití Storage Tables „Export Async“ s parametrem „changeSince“ (i když je označen jako deprecated), který vrací pouze změněné řádky od zadaného času.
  - Where filtry nejsou v SDK pro Export Async podporované; dokumentace a skripty musí sladit chování a omezení SDK.
  - Udržovat „state“ s timestampem posledního běhu na serveru; další běhy volají API s „changeSince“ a partitioning (po hodinách) se provádí následně na serveru.
  - Inkrement je nastaven jen u vybraných tabulek (např. company snapshot); jinde dochází k nadměrnému stahování kvůli omezením API (filtrace podle „datum“ sloupce není ekvivalent timestampu).
  - Návrh struktury: aktuální data po hodinách (jemná granularita, ~8700 souborů/rok per tabulka), historická data po měsících; AirSync by měl zvládnout velký počet souborů.
- Automatizace, notifikace a prostředí (Telegram, Slack, venv, crontab, cooldown)
  - Telegram bot: postup „start“ → kód → „verify“; ukládání uživatele („telegram users“), příkazy „help“, „who am I“, „test“, „status“ (spuštění reportu tlačítkem).
  - Slack notifikace nevyžadují linkování (autorizace přes Kebula účet); plán doplnit Slack vedle Telegramu.
  - NotifyRunner běží pod uživatelem, loguje a spouští skripty z jeho home; v crontabu naplánované odeslání (např. 7:30) neproběhlo kvůli aktivnímu cooldownu; po smazání state zpráva dorazila.
  - Cooldown brání spamování; konfigurovatelný (např. denní), v pilotu lze dočasně vypnout. Složka .notifications obsahuje logs a state; příkaz „notification state pgDailyReport“ ukazuje poslední odeslání.
  - Důraz na identické venv lokálně i na serveru (stejné balíčky); doplnit dokumentaci (Docs Notifications MD, CloudMD) k procesu tvorby/aktivace venv a umístění balíčků v uživatelském home.
  - V rámci FS oprávnění zmiňován sticky bit v /tmp; řešit omezení vůči Telegram bot skriptu a dočasným souborům.
- Bezpečnost, oprávnění a Cloud Settings
  - Cíl: read-only přístup pro cloud agenta do serverových složek; v CloudMD definováno „server read-only“ a „never modify“ pro vybrané adresáře; ověřit v praxi.
  - Rozlišení: Cloud MD (serverem spravované instrukce, auto-update ze serveru) vs. Cloud Local MD (uživatelské, nikdy nepřepisovat); implementovat zálohu/sync Cloud Local MD, aby se nevytratily jedinečné instrukce.
  - Nastavení Cloud Settings: zpřísněné čtení, explicitní „write and view credentials“ pro secrety, „write a edit“ na server; auditovat změny oprávnění.
  - Vytvořit settings.json v init skriptu: politika Allow (běh vybraných serverových skriptů, git fetch/status/tag apod.), Deny (ENV, credentials, secrets, PEM, keystore, password, token, API keys), Ask (reset, clean, push/force-push, RMM). Připravit GitHub issue, přiřadit Daša Dama, začlenit do initu prostředí.
- Zálohy, disky a provozní infrastruktura
  - Stav disků: root ~10 GB (jen ~3 GB volné), data ~30 GB (/data). Plán přesunout uživatelské homy na samostatný disk (např. sdc) nebo na /data; řešit velikost venv (stovky MB na uživatele).
  - Google snapshoty pro obnovu do 10 dnů; deploy script z GitHubu obnoví konfiguraci (sudoers atd.). Potřeba definovat retenci/frekvenci snapshotů, logování a DR postupy.
- Data pipeline: náklady, Parquet/DuckDB a typování
  - Směřování k parquet + lokální DuckDB, minimalizace Snowflake tabulek; minutové/dvouminutové refreshy ve Snowflake/Kebula mohou být nákladné (řádově tisíce USD/měsíc), lokální přístup je levnější/rychlejší.
  - Offload výpočtu na klienta: rozdílné refresh intervaly dle uživatelů (např. sekundové vs. 20 minut), inspirace praxí (Carvago: Snowpark + DuckDB).
  - Podpora Parquet v Kebule: možnost získat Parquet přes debug mode (file=parquet) v input mappingu; chybí jemná kontrola granularit exportu a jsou otázky kolem datových typů.
  - Doporučení zajistit top-notch Parquet soubory s konzistentním castingem typů, neodsouvat typové opravy pouze na klienta.
- Workflow, code review a operativa
  - Zavést práci v branchích a PR review, aby se předešlo chybám (např. nesprávné kontroly dat, parametry Change Since).
  - Ověřit a zdokumentovat cesty/názvy (test rsync vs. test sync, absolutní cesty).
  - Komunikace: preferovat rychlé reakce přes WhatsApp; Slack spíše okrajově.
  - Telemetrie: přichází pomalu; ověřit kvalitu a následně synchronizovat, definovat validační pravidla a monitoring.
## Další kroky
- [ ] Upravit „sync data“ pro synchronizaci skriptů a dokumentace ze serveru; zvážit obousměrnost pro User foldery.
- [ ] Navrhnout mechanismus ochrany uživatelských úprav v CloudMD (merge, overrides, verzování).
- [ ] Přidat uživatelskou identifikaci/namespace do User/scripts; definovat pravidla publikace (opt-in, whitelisting).
- [ ] Zavést ochranu/lock pro serverové složky (Scripts, Sync Script), nastavit cloud permissions (server read-only/never modify), otestovat v agentovi.
- [ ] Nahradit „check freshness“ AirSyncem; opravit oprávnění zápisu pro generátor metadat; standardizovat .metadata/JSON formát a umístění.
- [ ] Přidat do AirSync parametr „--delete“; připravit test plány pro velké adresáře (rename vs. delete).
- [ ] Ověřit SDK Kebula Storage pro podporu „changeSince“; upravit skript na inkrementální export přes „Export Async“; zavést a ověřit „state“ s timestampem na serveru.
- [ ] Nastavit 15minutové refresh intervaly s „changeSince“; definovat granularitu: aktuální měsíc hodinově, historie měsíčně; zdokumentovat.
- [ ] Doplnit dokumentaci k venv (init/activate, umístění balíčků v home) do Docs Notifications MD a CloudMD; sjednotit venv mezi lokálem a serverem.
- [ ] Zdebagovat crontab (časové zóny, cooldown stav); zvážit dočasné vypnutí/úpravu cooldownu v pilotu; nastavit alerty při selhání NotifyRunneru.
- [ ] Ověřit Telegram verifikaci a „test/status“ příkazy pro všechny uživatele; doplnit podporu Slacku v notifikacích.
- [ ] Vytvořit GitHub issue se settings.json, přiřadit Daša Dama; začlenit politiku Allow/Deny/Ask do initu prostředí; zavést audit změn oprávnění.
- [ ] Implementovat zálohu/sync Cloud Local MD; přesunout uživatelské homy na nový disk (/data nebo sdc); nastavit snapshot politiku (retence, frekvence, obnova).
- [ ] Zlepšit kvalitu Parquet typování; definovat odpovědnost za konzistentní casting datových typů.
- [ ] Zavést pravidelné code review, pracovat v branchích; zdokumentovat správné cesty/názvy; validovat telemetrická data před synchronizací.
- [ ] Otestovat inkrementální export na velkých tabulkách (timestamp filtrace); definovat backtracking window a testovací strategii, aby se předešlo nechtěnému full loadu.
## AI doporučení
> 1. Strategie aktualizace CloudMD bez přepisování uživatelských změn (verzování, diffs, templating) potřebuje konkrétní návrh a implementaci.
> 2. Jednoznačná identifikace/izolace uživatelských skriptů (uživatelské ID, namespaces) v adresářové struktuře je nutná, aby se předešlo nechtěné distribuci.
> 3. Nastavení směru a pravidel synchronizace User folderů (jednosměrně vs. obousměrně, konfliktní řešení) vyžaduje standard a testy.
> 4. Ochrana kritických serverových skriptů (locky, práva, CI/CD přepis) a audit oprávnění musí být jasně definována a ověřena.
> 5. Parametr „changeSince“ je deprecated; ověřit aktuální doporučený ekvivalent a plán kompatibility do budoucna.
> 6. Standardizovat formát/umístění metadat (.metadata/JSON), doplnit monitoring/alerting pro selhání zápisu.
> 7. Definovat granularitu exportů napříč tabulkami (aktuální vs. historická data) a technickou realizaci mimo Snowflake.
> 8. Minimalizovat IO zátěž při --checksum (plánování běhů, rate limiting); vyhodnotit dopady na velké datasety.
> 9. Nastavit testovací strategii pro inkrementální export (validace timestampu, pozdní data/backfill, prevence full exportu omylem).
> 10. Sjednotit dokumentaci s reálným chováním SDK/API (endpointy, omezení where filtrů).
> 11. Definovat politiku cooldownu v pilotu a produkci; nastavit alerty pro NotifyRunner/migrace (fallback scénáře).
> 12. Vyjasnit integraci Slack vs. Telegram v jednotné konfiguraci notifikací.
> 13. Stanovit odpovědnost za typování Parquet a kvalitu dat; plán integrace DuckDB backendu do Kebuly (milníky, feasibility).