### Akční položky
- @Padák - Specifikovat a implementovat mechanismus, který automaticky směruje artefakty do „User/Artifacts“ - [Termín neuveden].
- @Speaker 2 - Zpomalit scrollování a otevřít si podklady - [Termín neuveden].
- @Speaker 2 - Definovat/uložit správný cílový folder pro instalaci requirements v „initě“ - [Termín neuveden].
- @Padák - Doplnit dokumentaci (Docs Notifications MD) o cooldown hodnoty a případně o informace k venv - [Termín neuveden].
- @Padák & @Speaker 2 - Otestovat režim read-only pro serverové složky v souladu s „directory structure“ (never modify) - [Termín neuveden].
- @Padák - Opravit venv v souvislosti s úpravami Telegram notifikačního skriptu a nastavení permissions v /tmp - [Termín neuveden].
- @Speaker 2 - Zrevidovat, co se reálně děje při updatu dat z Kebuly (GIDA píšuje přiřazena Speaker 2) - [Termín neuveden].
- @Speaker 2 - Doplnit synchronizaci „Cloud Local MD“ z lokálu na server v rámci „sync data“, umístit v home uživatele - [Termín neuveden].
- @Padák - Předělat umístění user homů na samostatný disk (např. SDC) a nastavit snapshotování; připravit změny dle research tasku - [Termín neuveden].
- @Speaker 2 - Doplnit úpravy oprávnění do deploy skriptu podle dříve provedených ručních změn - [Termín neuveden].
- @Speaker 2 - Ověřit, jak se systém chová při neexistenci souboru metadata (zda má zastavit nebo zvolit alternativní běh) - [Termín neuveden].
- @Padák - Přehodnotit použití „check freshness/check-freeze“ vzhledem k chybějícím metadatům a dlouhému běhu - [Termín neuveden].
- @Team - Zvážit změnu pořadí kroků: spustit check freshness jako první a jasně definovat, kdy se provádí AirSync pro stažení metadat - [Termín neuveden].
- @Team - Prošetřit, proč AirSync synchronizuje „všechno“ (resp. příliš mnoho), a identifikovat příčinu nadměrných přenosů - [Termín neuveden].
- @Padák - Prověřit, zda API skutečně nepodporuje filtraci na timestamp změny a zda existuje alternativa - [Termín neuveden].
- @Speaker 2 - Ověřit, kde jsou var filtry z Data Description skutečně aplikovány v API callu - [Termín neuveden].
- @Speaker 2 - Přesměrovat implementaci na použití Export Async s „change since“ (timestamp) místo var filtrů - [Termín neuveden].
- @Padák - Implementovat parametr „--delete“ do AirSync - [Termín neuveden].
- @Speaker 2 - Obnovit timestamp a opravit parametr endpointu - [Termín neuveden].
- @Speaker 2 - Pracovat na telemetrických datech (dokončení, kontrola správnosti, příprava k synchronizaci) - [Dnes].
- @Speaker 2 - Pracovat ve vlastní branchi a vytvořit pull request k review - [Termín neuveden].
- @Padák - Provést code review pull requestu od Speaker 2 - [Po vytvoření PR].
- @Daša Dama - Zařadit navržené Cloud Settings do původního initu prostředí - [Termín neuveden].
- @Speaker 3 - Provést review dotazů (queries) k Infrastructure Cost Data (Lucka) - [Dnes].
- @Matěj - Připravit certifikované queries pro „top odpovídačku“ - [Příští týden].
- @Speaker 2 - Prověřit a řešit tok Sync + inkrementální stahování; podívat se na dotaz („kverinu“) týkající se „kostů“ - [Termín neuveden].
- @Speaker 2 - Zkontrolovat a případně převzít aktualizační skript týkající se „banner available data“ - [Termín neuveden].
- @Speaker 2 - V případě nejasností se doptat na konkrétní změny v aktualizačním skriptu - [Termín neuveden].
- @Speaker 3 (tým) - Posílat urgentní žádosti (odpovědi, review PR) na WhatsApp kvůli rychlejší reakci - [Termín neuveden].
### Klíčová rozhodnutí
- Změnit rozsah „sync data“ tak, aby zahrnoval nejen data, ale i skripty a dokumentaci — Odůvodnění: jinak se změny k uživatelům nedostanou.
- Přesměrovat „sync“ na plnou synchronizaci obsahu (skripty, dokumentace, instrukce) — Odůvodnění: manuální postup (rsync) není škálovatelný.
- Nešířit uživatelské skripty a artefakty plošně všem — Odůvodnění: zachovat izolaci uživatelského obsahu a zabránit nechtěné distribuci.
- Doplnit informace do CloudMD ohledně potřebných instrukcí — Odůvodnění: záměr „stačí to napsat…“, i když chybí část o serveru.
- Použít Cloud settings permissions k zákazu editací serverových složek (read-only přístup) — Odůvodnění: lepší než spoléhat na text v CloudMD.
- Zavést rozlišení mezi CloudMD (auto-updated from server) a Cloud Local MD (personal, never overwritten).
- Přesunout uživatelské homy na samostatný disk (SDB/SDC) a využít Google snapshoty pro zálohy — Odůvodnění: zálohovatelnost a kapacita.
- Potvrzení příčiny chyby metadat: chybějící write permission zabránilo zápisu metadat — Odůvodnění: logy z cron běhu ukázaly nenastavené permission.
- Timestampový sloupec je povinný pro podporu inkrementálních operací extraktorů/writerů — Odůvodnění: pro filtrování změn.
- Brát popis skriptu jako skutečné chování: timestamp existuje na úrovni API i když není v exportovaných datech.
- Preferovat jednoduchou strategii – export všeho při prvním běhu a poté jen změny podle timestampu uloženého ve state.
- Implementovat inkrementální exporty založené na timestampu a metadata state.
- Nepoužívat WHERE filtry přes SDK; využít správný endpoint pro inkrementální export (Export Async).
- Využít „change since“/timestamp mechaniku endpointu Export Async pro inkrementální exporty namísto var filtrů.
- Přidat parametr „--delete“ do AirSync — Odůvodnění: klientská složka musí odrážet stav serveru včetně mazání.
- Věci kolem GitHubu a okolí mají nízkou prioritu a mohou počkat — Odůvodnění: soustředit se na notifikace a desktopovou aplikaci.
- Parquet soubory budou udržovány ve vysoké kvalitě na zdroji; neprovádět castování datových typů až na klientovi — Odůvodnění: konzistence, méně práce u klientů, přímý přístup.
### Detailní zápis
[00:01-01:05] Uživatelský přístup k CloudMD a instrukcím je blokován, protože synchronizace přenáší pouze data, nikoli skripty a dokumentaci.
- Instalace je hotová pro více uživatelů (LabCone, Pavel, Jirka), Matěj Kis správně přidal CloudMD, ale konkrétní uživatel se k němu nedostane; „sync data“ synchronizuje pouze parquet soubory.
- Změny se nedostávají k uživatelům; regenerace „instructions“ (project.json → clod.md) se nepropaguje.
- Key Decision: „Sync data“ musí zahrnout i skripty a dokumentaci.
[01:05-01:29] Dočasně je možné řešit distribuci skriptů ručním spuštěním rsync, ale cílové řešení je, aby synchronizace přenášela vše ze serveru, nejen data.
- Ruční rsync nyní funguje; návrh dát postup do manuálu „clodových dat“.
- Key Decision: Plná synchronizace obsahu; manuální postup není škálovatelný.
[01:30-02:01] Synchronizace clod.md vytváří konflikt mezi aktualizací instrukcí a uživatelskými úpravami; přepisování souboru při každém syncu je problém.
- clod.md se regeneruje při každém stažení dat; uživatelské úpravy se přepisují.
- Identifikován druhý problém vedle nedostupnosti skriptů/instrukcí.
[02:01-03:14] Změny v notifikacích a potřebě obousměrné synchronizace odhalily riziko nechtěné distribuce uživatelských skriptů všem uživatelům.
- Lokální Python pro notifikace by se při nahrání mohl rozšířit všem; nežádoucí.
- Key Decision: Nešířit uživatelské skripty a artefakty plošně.
[03:15-03:44] Uživatel (Claude) iniciativně modifikoval serverový folder Scripts; pokud nezasáhne Sync Script, je to opravitelné, ale přesto nežádoucí.
- Změny by se při updatu přepsaly; jedná se o dvě nedomyšlené části (synchronizace a práva/izolace).
[03:45-04:23] Přechod k demo/ukázce: požadavek na sdílení obrazovky, potvrzení dostupnosti, a popis lokálního vs. serverového „home“.
- Diskuse o sdílení desktopu; ukázka rozdílu mezi lokálním a serverovým „home“.
[04:47-05:19] Struktura adresářů: rozlišení mezi „naše věci“ a „User“ obsahem; DuckDB je generován na klientovi a není synchronizován ze serveru.
- Server: Docs, Example, Metadata, Parked, Scripts; User: Artifacts, DuckDB, Notifications, Parked, Scripts.
- DuckDB vzniká na klientovi; není součástí serverového syncu.
[05:20-05:50] Definice „User/Artifacts“ a dotaz na mechanismus ukládání; zatím se ukládání řeší pouze informováním uživatele. (Sloučeno z 320904-349296 a 349395-350095)
- „Artifacts“ jsou uživatelské výstupy; otevření artefaktu zobrazí dashboard.
- Dotaz na mechanismus ukládání; zatím jen pokyn „kam ukládat“.
- Action Item: @Padák - Specifikovat a implementovat automatické směrování artefaktů do „User/Artifacts“.
[05:50-06:21] Přehled reorganizace složek a komponent: přesun „bordelu“ do Artifacts, vymezení Cloud MD (správa Matěj Kis), DugDB při inicializaci, Notifikace a Parkety.
- Cloud MD spravuje Matěj Kis; Notifikace mají vlastní složku; Parkety prázdné pro případ předpočítání.
[06:21-06:52] Popis aplikace pro pricing kalkulačky a uživatelských transformací dat mimo centrální update.
- Pavel postavil aplikaci; update dat neřeší; pipeline by mohla generovat další data.
[06:52-07:22] Mapování parity složek na serveru a u uživatelů; umístění složek users v home.
- Paritní serverová struktura; users ve vlastním home, data nikam neodcházejí.
[07:22-08:01] Kopie adresáře users v Padákově home, obsah Notifications a změny v ECOV; vysvětlení SyncData.
- SyncData: synchronizuje User Folder na server; Server Folder ze serveru.
[08:04-08:25] Omezení synchronizace: zatím neprobíhá sync ze serveru do lokálních User Folderů; sdílení browseru.
- Zatím jen pro to, aby bylo na serveru; lokální User Foldery se neaktualizují.
[08:38-08:41] Návrh: připojit se na KOL a domluvit si diskuzi.
- Krátký organizační návrh.
[08:50-09:21] Redesign dashboardu a jeho dynamická generace při update z Kebuly; jasná místa pro úpravy.
- Dashboard bez scrollu; dynamický generátor při update.
[09:21-09:39] Dashboard ukazuje last sync, compressed/uncompressed a Telegram notifications; přechod ke sdílení obrazovky.
- Telegram notifikace „unlinked“; potřeba sdílet screen.
[09:51-10:36] Autorizace notifikací: Slack vs Telegram; preference Telegramu.
- Slack autorizovaný Kebula účtem; preference Telegramu.
[10:38-11:07] Nastavení kebula data bot v Telegramu a navázání konverzace.
- Přidání bota, zahájení konverzace.
[11:08-12:16] Mechanismus linkování Telegramu pomocí kódu: generování, pending kody, identifikace uživatele.
- Generace kódu, pending kódy, verifikace; „vítej“ chybí.
[12:17-13:20] Stav po verifikaci: evidování uživatele v telegram users; test report a jeho výstupy.
- Uživatel evidován; „test“ posílá text+obrázek.
[13:27-14:02] Vysvětlení „test“: jednotný test spojení; „status“ vypisuje dostupné user notifications s tlačítkem.
- „Status“ spustí report; výstup definován ve skriptu.
[14:33-15:38] Automatické doručení reportu přes crontab, venv správa Klodem lokálně i na serveru; požadavek na identická prostředí.
- Venv musí být identický lokálně i na serveru; NotifyRunner běží z venvu.
[15:38-16:09] NotifyRunner běží pod uživatelem Petr a spouští skripty v jeho home; identifikace důvodu neproběhnutí notifikace.
- Důvod: design notifikačního systému.
[16:09-17:46] Design cooldown mechaniky v notifikacích: .notifications/state; omezení spamování; příklad CRM konektoru.
- Cooldown perioda; po smazání state dorazily data.
[18:13-19:08] Diskuze o komplikacích cooldownu v pilotu a Padákovo stanovisko k flexibilitě konfigurace.
- Cooldown volitelný; možnost migrací a fallback v sync scriptu.
[20:13-20:16] Shrnutí: aktuální setup, který Padák zavedl.
- Stručné shrnutí.
[20:17-24:28] Technické dotazy ke zrcadlení venv mezi lokálem a serverem; hledání init skriptů a vytvoření venv. (Sloučeno z 1217011-1328285, 1329185-1468035 a 1462655-1468035)
- Diskuse o tom, kdy a jak se na serveru vytvoří venv; hledání „python -m venv“ v initu; potvrzení, že je stažené; potřeba jasného postupu.
- Action Item: @Speaker 2 - Definovat cílový folder pro requirements v „initě“.
[24:29-25:18] Účastníci se vyjasňují nad inicializačním skriptem (init), rolí „Bula – Internal Data Analyst, Crypt“, formátováním a orientací ve skriptech.
- „Init“ a formátování; Padák nezkoumá detailně skripty ostatních.
- Action Item: @Speaker 2 - Zpomalit scroll a otevřít podklady.
[25:34-26:42] Je nutné vyřešit automatickou instalaci závislostí při nasazení notifikací, včetně správného umístění a práce s venv na serveru.
- Potřeba jasného procesu instalace; příklad chybějících balíčků.
[26:51-27:33] Padák navrhuje doplnit informace do CloudMD; současně konstatuje, že o serveru tam nic není.
- Key Decision: Doplnit instrukce do CloudMD (část o serveru chybí).
[27:58-28:45] Dokumentace k notifikacím obsahuje strukturu a postupy; zvažováno doplnění informací o venv.
- „Server Docs Notification MD“ a „Docs Notifications MD“; doplnit cooldown a venv.
- Action Item: @Padák - Doplnit dokumentaci.
[29:15-30:58] Diskuse o omezení práv (permissions) pro Clouda: read-only přístup do některých složek; zabránit editacím serverových skriptů.
- Dokumentace „directory structure“; nastavení v Cloud settings.
- Key Decision: Použít Cloud settings k zákazu editací; Action Item: test read-only režimu.
[33:14-34:20] Zavedení rozlišení mezi CloudMD (auto-updated) a Cloud Local MD (personal, never overwritten); plán na opravu venv a poznámky k Telegramu a permissions.
- Key Decision: Rozlišení MD; Action Item: @Padák - Opravit venv a permissions v /tmp.
[35:02-36:47] Optimalizace synchronizace: přidán „--checksum“ do SyncData; zátěž vs. přesnost; návrh syncovat i Cloud Local MD.
- Action Item: @Speaker 2 - Zrevidovat update flow; doplnit sync Cloud Local MD na server.
[37:49-40:35] Plán zálohování a disaster recovery: přesun homů na samostatný disk; snapshoty; kapacitní limity. (Sloučeno z 2269116-2433634 a 2434514-2435554)
- Key Decision: Přesun homů na SDB/SDC; Google snapshoty.
- Action Item: @Padák - Připravit přesun a snapshoty.
[40:36-41:47] Řešení nesrovnalostí v oprávněních a roli deploy skriptu při generování metadat; identifikace write-permission problému.
- Action Item: @Speaker 2 - Doplnit oprávnění do deploy skriptu.
- Key Decision: Příčina chyby: chybějící write permission.
[42:15-44:14] Účel metadat na straně uživatele; check freshness a absence .metadata JSON; exit code != 0; AirSync pořadí.
- Action Item: @Speaker 2 - Ověřit chování při neexistenci metadata.
- Action Item: @Padák - Přehodnotit check freshness; @Team - Spustit check freshness jako první.
[44:38-45:43] Analýza skriptu: chybí explicitní stažení; AirSync porovnává velikost a mtime; flexibilní definice „stáří“ dat.
- Diskuse o denních vs. hodinových kritériích; bez finálního rozhodnutí.
[46:25-47:11] Návrh vyřadit „check freshness“ a spoléhat na AirSync; otázka proč se synchronizovalo „všechno“.
- Action Item: @Team - Prošetřit nadměrné přenosy AirSync.
[47:24-50:58] Zjišťování rozsahu synchronizace; inkrementy na velkých tabulkách; omezení API; „To je divný.“ (Sloučeno z 2844196-3018360 a 3057430-3058450)
- API nepodporuje snadné timestamp filtry v datech; Padák má ověřit alternativy.
- Action Item: @Padák - Prověřit podporu timestamp filtrů v API.
[51:00-51:39] Uznání možné chyby na začátku; lokalizace skriptu v repozitáři; potřeba zjistit název a volání.
- Skript je v repozitáři; nutné dohledat přesné volání.
[51:56-52:59] Identifikace zdroje a komponenty: zdroj „www.hradeckralove.org“ a skript „DataSync“ ve složce „src“.
- Potvrzení názvu a umístění.
[53:01-54:22] Požadavek na detailní popis „DataSync“; důležitost timestampového sloupce; incremental vs. partition sync.
- Key Decision: Timestamp povinný; rozlišit incremental vs. partition; ověřit aplikovaný režim.
[54:41-57:06] Problém exportu celé tabulky bez timestampu z API vs. záměr timestamp filtrování; objasnění, že timestamp je na úrovni API.
- Key Decision: Popis skriptu jako zdroj pravdy; partition sync chybí timestamp v output; WHERE nad date sloupci.
[57:06-59:31] Var filtry vs. timestamp; plán znovu zkusit; preferovat jednoduchou strategii timestamp state.
- Key Decision: První běh full, pak změny podle timestamp.
[01:00:02-01:02:01] Workflow: full export → Parquet → state s timestampem → následně inkrementální exporty „větší než timestamp“.
- Key Decision: Implementovat inkrementální exporty podle timestampu.
[01:00:44-01:04:05] Identifikace endpointu; omezení SDK; použít Export Async a „change since“ (deprecated) pro timestamp. (Sloučeno z 3644882-3721116, 3771414-3845886 a 3841326-3845886)
- SDK nepodporuje WHERE filtry; „change since“ sahá na timestamp.
- Key Decision: Využít Export Async s „change since“; Action Item: @Speaker 2 - Přesměrovat implementaci.
[01:04:06-01:04:43] Návrh 15minutových refreshů dat s využitím ChangeSince; prázdné joby bez změn.
- Efektivní běh bez zbytečných přenosů.
[01:05:16-01:06:13] Organizace souborů po tabulkách a intervalech (měsíce vs. hodiny); dopad na objem; AirSync to zvládne; pozdější konsolidace.
- Začít jednoduše; konsolidovat později.
[01:06:18-01:07:49] Řešení mazání a plných reloadů při změnách dat; chování AirSync.
- Full delete + full export při mazání; AirSync srovná obsah.
[01:07:50-01:08:35] Oprava špatného parametru endpointu; nabídka testu AirSync na test serveru.
- Action Item: @Speaker 2 - Obnovit timestamp a opravit parametr; @Padák - Mock test AirSync.
[01:08:37-01:11:37] Rozsah synchronizace AirSync (data vs. dokumentace); aktualizace server MD; praktická demonstrace test rsync.
- AirSync synchronizuje vše v server folderu; test ukázal potřebu „--delete“.
[01:11:40-01:13:02] Plán dopracovat mazání v AirSync pomocí „--delete“; odklad detailů.
- Key Decision: Přidat „--delete“.
[01:13:05-01:14:34] Další postup práce na datech; PR workflow; telemetrie; potvrzení pokračování. (Sloučeno z 4385398-4473430 a 4473790-4474870)
- Action Items: PR a review; práce na telemetrii; komunikace.
[01:14:36-01:15:19] Konfigurace Cloud Settings s explicitními oprávněními; příprava testu.
- Nastavení deny/read; povolení write/view credentials/secret; test s novým „Clodem“.
[01:15:15-01:15:54] Rozdíl mezi „write“ a „edit“ a jeho dopady v praxi.
- Obě znamenají modifikaci; „edit“ může mít omezení.
[01:15:57-01:16:45] Test notifikací a úprav dokumentace (Telegram vs. Slack); zapsání „Telegram“ kapitálkami do dokumentu.
- Očekávání výsledku; „first line kontrola“.
[01:17:09-01:17:42] Nastavení, aby server skripty běžely bez potvrzení.
- Návrh v settings; bez záznamu o provedení.
[01:17:47-01:20:44] Návrh settings.json pro init prostředí; allow/deny seznamy; zásady pro přístup; přesun do GitHub Issue; rozšíření notifikací. (Sloučeno z 4667718-4766404 a 4792916-4844396)
- Allow: fetch/status/...; Deny: env/credentials/secrets/...; některé server akce „Ask“.
- Action Item: @Daša Dama - Zařadit Cloud Settings do initu.
[01:20:45-01:22:10] Experiment: macOS status bar aplikace pro zobrazování notifikací; instalační skript; cílení na CSU.
- Desktop notifikace; 15min updaty jako atraktivní.
[01:22:28-01:23:24] Cloud Learnings; úkol pro Mañana/Anneli; review Infrastructure Cost Data queries.
- Action Item: @Speaker 3 - Review dnes.
[01:23:25-01:29:57] Architektonické doporučení: minimalizace Snowflake nákladů; využití parquet + DuckDB; offload na klienty.
- DuckDB rychlé; snížit závislost na Snowflake; stream costů možný, zatím bez use-case.
[01:27:59-01:29:57] Diskuse o podpoře parquet v Kebule; exportní možnosti; omezení UI/debug mode.
- Parquety přes debug mode; časové členění neovlivnitelné; ponechat stav; výpočty na klientovi.
[01:30:41-01:31:22] Stabilizace a certifikace queries; jejich role pro „Cloda“.
- Action Item: @Matěj - Certifikované queries příští týden.
[01:31:22-01:32:23] Castování typů na klientovi vs. kvalita parquet; závěr segmentu.
- Key Decision: Kvalita parquet na zdroji; necastovat na klientovi.
[01:32:24-01:32:55] Ukončení předchozí části; priority: notifikace; nízká priorita GitHub okolí; odstranit „check sam“ ze Sync scriptu; potvrzení.
- Key Decision: Nízká priorita pro GitHub okolí.
[01:32:55-01:33:53] Plán: řešení Syncu s inkrementálním stahováním; „kosty“ query; aktualizační skript „banner available data“; komunikační preference; ukončení hovoru.
- Action Items: @Speaker 2 - Sync + inkrementální; kontrola banner skriptu; dotazování na změny; @Speaker 3 - WhatsApp pro urgentní; rozloučení.