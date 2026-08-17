# Sémantická vrstva: sekvenční plán dotažení k paritě

**Datum:** 2026-08-16
**Stav:** návrh k odsouhlasení, pre-implementace
**Ověřeno proti:** `main` @ 8988f46, verze 0.83.25
**Navazuje na:**
- `docs/superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md` (kontrakt úložiště — *contract spec*)
- `docs/superpowers/specs/2026-08-14-semantic-layer-ui-and-agent-parity-design.md` (UI + agentní parita — *parity spec*, schválený design, neimplementovaný)

Tenhle dokument neotevírá znovu nic, co ty dva specy rozhodly. Přidává
**pořadí, cutover-postup a done-kritéria** — a opravuje pět faktů, na kterých
původní zadání stálo.

---

## 0. Korekce vstupního obrazu

Zadání bylo psané pro session bez přístupu k repozitáři. Pět jeho premis
neodpovídá `main`; dvě z nich mění závěr, ne jen formulaci.

**K0.1 — §5 („co chybí") není neprozkoumané pole, je to schválený design.**
`2026-08-14-semantic-layer-ui-and-agent-parity-design.md` pokrývá UI (tři
úrovně, pět tabů, `fields[]` tabulka, AI blok s anti-keywords), editaci po
objektech s optimistickým zámkem na `content_hash`, `validate_semantic_query`
na všech třech povrchách, čtecí trojici, skill `semantic-layer-building`
i sekci v generovaném `CLAUDE.md`. Plán níže tedy **nespecifikuje**, co
postavit — sekvencuje, kdy.

**K0.2 — nejsou to dvě paralelní pipeline. Je to jeden fetch a dvojí zápis.**
Tohle je nejdůležitější korekce. `connectors/keboola/semantic_ossie.py`
(393 řádků) existuje, je registrovaný jako adaptér `keboola_metastore`
(`src/semantic/adapters/__init__.py`) a `sync_semantic_layer` ho už volá
(`semantic_layer.py:926-929`): skládá plné Ossie dokumenty a ukládá je do
`semantic_models` pod `source="keboola_metastore"`. Dokumentová cesta je
**živá a plnohodnotná** — nic se na vstupu nezahazuje, včetně věcí, pro které
plochá tabulka nemá sloupec (per-field popisy, deklarovaný dialekt,
relationships, `ai.keywords`, constraints, glossary).

Souběžně běží legacy plochý zápis do `metric_definitions` pod
`source="keboola_semantic_layer"`. Zbývající práce tedy není „migrovat
pipeline", ale **cutover ploché tabulky na jednoho writera**.

A není to nezmapované území: docstring `_store_ossie_documents`
(`semantic_layer.py:681-706`) tenhle cutover pojmenovává jako plánovaný úkol
a dokumentuje, proč zatím neproběhl — obě pořadí zápisu byla vyzkoušena
a obě selhala (kompozice před plochou smyčkou → její name-ownership check
řádek tiše přeskočí; kompozice po ní → druhý, jménem duplicitní řádek pod
jinou `source`, protože `metric_definitions` má unikátnost jen na `id`, ne na
`name`).

**K0.3 — MCP má už dva nástroje, ne jeden.** `semantic_model_search`
i `semantic_model_get` jsou v `foundation_tools.py:60-61`. `semantic_model_get`
vrací celý dokument byte-for-byte, což pokrývá podstatnou část toho, co dělá
`get_semantic_context`. Chybí tedy hlavně *typovaný výběr* (nenačítat celý
dokument kvůli jedné metrice) a `get_semantic_schema`.

**K0.4 — nová cesta má vlastní vadu, kterou zadání nezná, a je horší než N4.**
`src/semantic/projection.py:143-158` zapisuje metriku takto:
- `sql=` **holý fragment** z `resolve_expression` (`SUM(amount)`), bez
  `SELECT`/`FROM`;
- `table_name` **nepředává vůbec** → v ploché tabulce žádná vazba na tabulku;
- `grain` nepředává → uplatní se default `grain: str = "monthly"` v
  `src/repositories/metrics.py:45` i `metrics_pg.py:42` (a `DEFAULT 'monthly'`
  na sloupci, `src/db.py:551`).

Legacy cesta přitom skládá spustitelné `SELECT <fragment> FROM "view" AS t`.
Takže: **naivní cutover na `project_document` je funkční regrese** —
z runnable SQL na nespustitelný fragment. A `grain="monthly"` je *vymyšlená*
hodnota, ne zkopírovaná; to je horší než N4, protože N4 aspoň kopíruje něco
pravdivého o datasetu. `agnes catalog --metrics --show` tiskne `Grain:`
bezpodmínečně, takže tahle lež se k agentovi dostane i textovou cestou.

**K0.5 — cutover má cestu ven, protože adaptér už nese, co projektor
potřebuje.** `semantic_ossie.py` ukládá do `custom_extensions` pod vendor
jménem `AGNES` právě ta pole, pro která Ossie nemá slot: vazbu metriky na
dataset (`_compose_metric`, ř. 258 — `{"dataset": dataset_table_id}`),
`grain` datasetu (ř. 196-200) a constraints na úrovni modelu (ř. 337-339).
Ossie `Metric` má `additionalProperties: false` a **žádný odkaz na dataset**,
takže bez téhle extension by spustitelné SQL složit nešlo vůbec.
Projektor tuhle extension jen zatím nečte.

**To je celý cutover v jedné větě:** naučit `project_document` číst `AGNES`
extension → tím dosáhne parity s legacy composerem → teprve pak legacy
composer smazat.

**K0.6 (drobné, pro úplnost).** N1: `models[0]` je na ř. 1822-1823 (coverage
report) a ř. 730; ten druhý je uvnitř `_store_ossie_documents` a je
**korektní** — bere jméno modelu z právě validovaného jednomodelového
dokumentu, ne z listu všech modelů. Opravovat se má jen coverage report.
N2 potvrzeno: `_show_one_metric` (`cli/commands/catalog.py:124-150`) tiskne
ID/Name/Display/Category/Type/Unit/Grain/Table/Description/SQL/Synonyms/Notes
— `validation` mezi nimi není; `dimensions` také ne (takže z N4 uniká textovou
cestou jen `grain`, `dimensions` jen přes `--json`).

---

## 1. Rozhodnutí: dvojí zápis doběhnout, ne nechat žít (odpověď na Q2)

**Rozhodnutí: doběhnout. Dokument je kánon, `metric_definitions` má mít
jednoho writera — `project_document`.**

Proč ne koexistence:

- **Koexistence není levná, je jen tichá.** Každá oprava v legacy composeru
  (N1, N4, N5) je práce do kódu, který má zmizet. ~1000 řádků mapovací logiky
  (`build_metric_row`, `try_join_composition`, `compose_sql`,
  `merge_constraints`, `references_foreign_alias`, …) se udržuje paralelně
  s projektorem, který dělá totéž pro každý jiný zdroj.
- **Rozdíl mezi cestami je dnes uživatelsky viditelný.** Keboola metrika má
  spustitelné SQL a vazbu na tabulku; git-backed Ossie metrika má fragment
  a `NULL` tabulku a vymyšlený `monthly` grain. Agent čte obě přes stejný
  `agnes catalog --metrics`. To není „dvě cesty", to je nekonzistentní
  produkt.
- **Legacy composer je strop fidelity.** Plochá tabulka nemá kam uložit
  dataset — to je kořen N4. Dokud je writerem, N4 nejde opravit, jen
  zamlčet.

Proč ne „prostě přepnout":

- Regrese z K0.4 (fragment místo runnable SQL, chybějící `table_name`).
- Race z K0.2, reprodukovaná v obou směrech.
- Změna `metric_definitions.id`: `keboola/{model_uuid}/{name}` →
  `{source}/{source_ref}/{model_name}/{name}` (`_scoped_id`).

### Bezpečný postup: pět kroků, cutover až ve čtvrtém

**C1 — Zavřít fidelity gap projektoru.** `project_document` čte `AGNES`
extension: `dataset` → `resolve_table_name` proti `table_registry` → naplní
`table_name` a složí runnable `sql`; `grain` → předá se explicitně místo
defaultu; `constraints` → naplní `validation`. Zároveň
`metric_repo().create(grain=...)` přestane defaultovat na `"monthly"`
(`Optional[str] = None`) na **obou** backendech — v `src/`, `app/`, `cli/`,
`connectors/` je `project_document` jediný volající `.create()`, takže dosah
je malý a měřitelný.
*Beze změny chování pro dnešní data: Keboola dokumenty se zatím neprojektují.*

**C2 — Shadow mode a diff.** `sync_semantic_layer` projektuje Keboola
dokumenty pod `source="keboola_metastore"` **vedle** legacy řádků. Prune je
scoped na `(source, source_ref)` v obou writerech
(`projection.py:202-223`), takže si navzájem řádky smazat **nemohou** — to je
právě ta vlastnost, která dělá shadow mode bezpečným. Duplicita podle `name`
je v tomhle kroku očekávaná a dočasná; konzumenti (katalog, `agnes catalog
--metrics`) v shadow módu filtrují `keboola_metastore` pryč.
Výstupem je **golden regression diff**: řádek po řádku legacy vs. projektor.

**C3 — Diff na nulu.** Každý rozdíl je buď chyba projektoru (opravit), nebo
vědomé vylepšení (zapsat do allow-listu s odůvodněním — typicky metriky,
které legacy skipoval jako `foreign_alias_reference`, a projektor je zvládne,
nebo naopak). Cutover se nespouští, dokud diff není prázdný nebo celý
odůvodněný.

**C4 — Cutover.** Legacy plochý zápis vypnut, projektor přebírá. Jedna
transakce: smazat řádky `source="keboola_semantic_layer"` a zapsat projekci.
Riziko změny `id` je **nízké a ověřené**: v repu neexistuje žádná FK ani
RBAC vazba na `metric_definitions.id` (metriky nejsou `ResourceType`, žádný
grant ani data package je nereferencuje). Zbývá jen lidská/agentní paměť na
`--show keboola/<uuid>/<name>`. Doporučení: **přijmout změnu id** a ošetřit ji
hintem v `cli/query_hints.py` na 404 („metric not found; ids changed in
0.8x — try `agnes catalog --metrics | grep <name>`"), ne dvojí-id
kompatibilní vrstvou.

**C5 — Smazat legacy composer** a jeho testy. Coverage report se v tomhle
kroku přepojí na dokument (viz vlna 3).

---

## 2. Rozhodnutí o N4: varianta (a), rozšířená (odpověď na Q3)

Zadání nabízí (a) neimportovat, (b) importovat s označením původu,
(c) nechat být.

**Volím (a) — s tím, že se týká i nové cesty a je širší, než zadání tušilo.**

Rozhodující je dopad na agenta, který čte plochou projekci. Ten agent nemá jak
poznat, že `Grain: monthly` na metrice `SELECT SUM(amount) FROM t AS t` je
obtisk datasetu (legacy) nebo tovární default (nová cesta). Nedostane
varování — dostane **tvrzení**. A `cli/templates/global_rails.md:15` ho na
tenhle příkaz posílá s instrukcí „Never invent metric SQL", tedy s explicitní
nálepkou autority. Špatná hodnota pod nálepkou autority je horší než chybějící
hodnota: chybějící pole agenta donutí zeptat se nebo si to odvodit z dat,
špatné ho utvrdí.

Proti (b): „označení původu" je odpověď na otázku *kde se to vzalo*, ale
problém je *je to pravda o téhle metrice*. `grain` datasetu prostě není
`grain` metriky; badge „zděděno z datasetu" tu nepravdu nezruší, jen ji
opatří poznámkou pod čarou, kterou textový výpis stejně netiskne.
Proti (c): dnešní stav je aktivní klamání, ne mezera.

Konkrétně:

1. `metric_repo().create` přestane defaultovat `grain="monthly"` (obě
   repa, viz C1). **Nejvyšší priorita z celého N4** — týká se každé
   Ossie-importované metriky na každé instanci.
2. Projektor `grain` **zapisuje jen tehdy**, když ho `AGNES` extension
   skutečně nese, a nese ho *dataset*. Pokud metrika k datasetu vázaná je
   (přes `custom_extensions.dataset`), je zdědění grainu obhajitelné —
   ale pak patří na `notes`, ne na `grain`. Doporučení: **`grain` u
   importovaných metrik nechat prázdný**, dataset grain přenést jako
   `notes` položku ve tvaru `dataset grain: monthly`. Agent tak dostane
   fakt i jeho rozsah.
3. `dimensions` z `primaryKey` se **neimportuje**. Primární klíč datasetu
   nejsou dimenze metriky; nikde se netiskne textově a přes `--json` jen
   škodí.
4. Plná pravda (grain datasetu, jeho `primaryKey`, `fields[]`) zůstává
   dostupná — v dokumentu, přes `semantic_model_get` a přes UI z vlny 3.
   Tohle je ta část, kvůli které (a) není ztráta informace, ale její
   přesun tam, kde je pravdivá.

---

## 3. Priorizace do vln (odpověď na Q1)

Kritérium: **hodnota na jednotku rizika**, kde „hodnota" = o kolik míň bude
agent nebo admin uveden v omyl, a „riziko" = kolik povrchů se musí změnit
najednou. Z toho plyne pořadí: nejdřív *přestat lhát o tom, co už existuje*,
pak *sjednotit writera*, pak *stavět nové povrchy*.

Explicitní důvod, proč pravdivost předchází novým povrchům: každý nový povrch
(UI, čtecí trojice, validátor) čte tatáž data. Postavit je nad daty, která
tvrdí `grain: monthly` o metrice bez časové dimenze, znamená tu chybu
zreplikovat do pěti míst a opravovat ji pětkrát.

### Vlna 0 — pravdivost stávajících povrchů (dny, ne týdny)

Tři nezávislé opravy, každá ≤1 soubor, žádná změna schématu, žádná závislost
na cutoveru — a všechny **přežijí** cutover, takže to není throwaway práce.

| # | Co | Kde |
|---|---|---|
| 0.1 | Coverage report přes všechny modely, ne `models[0]` | `connectors/keboola/semantic_layer.py:1822` |
| 0.2 | `validation` (constraints) do textového výpisu metriky | `cli/commands/catalog.py:_show_one_metric` |
| 0.3 | Zrušit default `grain="monthly"` v obou repech | `src/repositories/metrics.py:45`, `metrics_pg.py:42` |

Proč právě tyhle tři první: 0.2 je jediná oprava, která agentovi doručí
pravidlo, jež na metrice platí, a je to jednořádkový přírůstek do funkce, co
už osm dalších polí tiskne. 0.1 je admin-facing report, který dnes popisuje
jeden model a o zbytku mlčí, přičemž importér už všechny modely zpracovává —
report tedy protiřečí chování vedle sebe stojícího kódu. 0.3 je nejmenší
možný zásah s nejširším dosahem (každá Ossie metrika na každé instanci).

**Hotovo, když:**
- Coverage report nad fixture se dvěma modely vrací dvě `sources[].model`
  položky (dnes jednu) a součty `importable`/`unregistered` pokrývají metriky
  obou modelů. Test musí nejdřív selhat na neopraveném kódu.
- `agnes catalog --metrics --show <id>` na metrice s constraints vypíše
  sekci s pravidlem a jeho severity; test asserted na *obsah řádku*, ne na
  exit code.
- `metric_repo().create(...)` bez `grain` uloží `NULL`, ne `"monthly"` —
  kontraktní test v `tests/db_pg/` parametrizovaný přes oba backendy.

### Vlna 1 — projektor dosáhne parity (C1)

Fidelity gap z K0.4 + rozhodnutí N4 z §2. Žádné vypínání legacy cesty.

**Hotovo, když:**
- Ossie dokument nesoucí `AGNES` extension s `dataset` projektuje metriku
  s neprázdným `table_name` a se `sql`, které **začíná `SELECT` a obsahuje
  `FROM`** — tzn. je spustitelné, ne fragment.
- Dokument **bez** té extension (čistý upstream Ossie, git zdroj) projektuje
  dál fragment — a to je vědomé; test to tvrdí explicitně, aby to nikdo
  později „neopravil".
- Metrika s constraints v `custom_extensions` má neprázdné
  `metric_definitions.validation`, a vlna 0.2 ho zobrazí (end-to-end přes obě
  vlny).
- Žádná projektovaná metrika nemá `grain` jinak než z datasetu, a pak jako
  `notes`, ne jako `grain`.

### Vlna 2 — shadow, diff, cutover (C2–C5)

Nejrizikovější vlna a jediná, která mění data existujících instalací. Proto
až po vlně 1: bez parity projektoru by diff byl nečitelný (stovky rozdílů
z fragmentů a `NULL` tabulek by přehlušily skutečné mapovací rozdíly).

**Hotovo, když:**
- **Golden regression:** fixture Metastore projektu (≥2 modely, metrika
  s JOINem přes relationship, metrika se `SNOWFLAKE`-only dialektem, metrika
  s constraintem, metrika na neregistrované tabulce) projde oběma writery
  a diff je prázdný — nebo každý zbylý rozdíl je v allow-listu s napsaným
  důvodem. Tenhle test je *artefakt cutoveru*, ne jeho příprava: zůstává
  v repu jako regrese i po smazání legacy composeru (fixture zafixuje
  očekávaný výstup).
- Po cutoveru: `SELECT count(*) FROM metric_definitions WHERE
  source='keboola_semantic_layer'` = 0 a počet řádků pod
  `source='keboola_metastore'` ≥ původní počet.
- `agnes catalog --metrics` na instanci s Keboola i git zdrojem vrací
  metriky **stejného tvaru** z obou (obě mají `table_name`, obě mají
  runnable `sql` nebo obě deklarovaně nemají) — to je vlastní důkaz, že
  writer je jeden.
- `connectors/keboola/semantic_layer.py` je kratší o mapovací vrstvu;
  `grep -c "def " ` klesne o funkce vyjmenované v C5. (Pozorování, ne test.)

### Vlna 3 — validátor dostane převodovku

`src/semantic_validation.py` má 724 řádků, 77 testů, sedm kol review a nula
volajících. Je to nejlevnější velký přírůstek hodnoty v celém plánu: motor je
hotový, chybí REST + CLI + MCP obal podle parity specu §5. Zároveň je to
první věc, která agentovi umožní **něco ověřit před spuštěním**, ne jen si
přečíst.

Proč až tady a ne dřív: validátor čte constraints z dokumentu. Dokud jsou
constraints pravdivě dostupné jen jednou cestou (vlna 1) a dokud jsou modely
konzistentní (vlna 2), vracel by odpovědi závislé na tom, kterým writerem
metrika prošla.

**Hotovo, když:**
- Dotaz porušující `error`-severity constraint vrátí `valid=false`
  a violation přes **všechny tři** povrchy se shodným tvarem (REST, CLI,
  MCP) — parity ratchet to hlídá.
- Metrika, jejíž jediné výrazy jsou v jiném dialektu než DUCKDB/ANSI_SQL,
  dá `locally_executable=false` s warningem — to je přímý test na past,
  kterou dnes objeví až analytik.
- Staticky neověřitelné pravidlo skončí v `post_execution_checks`, **ne**
  jako `valid=false` (fail-open vs. fail-closed je tu vědomá volba a test ji
  fixuje — dvě z deseti chyb nalezených v review tohohle modulu byly přesně
  tohle).
- Instance bez jediného validního modelu nástroj vůbec nenabízí (fail-closed
  gating).

### Vlna 4 — UI a agentní parita

Zbytek parity specu: `/semantic-layer` list → model s pěti taby → detail
objektu, čtecí trojice (`get_semantic_context` typovaný výběr,
`get_semantic_schema`), skill `semantic-layer-building`, sekce v generovaném
`CLAUDE.md`. Read-only pro importované modely, editace jen pro native.

Poslední, protože je to největší kus práce a **jediný, jehož absence nikoho
neuvádí v omyl** — jen chybí. Vlny 0–2 opravují nepravdy, vlna 3 odemyká
hotový motor; UI je přírůstek pohodlí nad daty, která už jsou správná.

**Hotovo, když:**
- Detail datasetu vyrenderuje `fields[]` jako tabulku Name/Type/Role/
  Description a `ai` blok v pěti skupinách **včetně anti-keywords** (to je
  jediný negativní signál v celé vrstvě; když vypadne, je to tichá ztráta).
- Importovaný model nemá edit affordance a mutace na něm vrací `409
  source_owned` na REST i MCP.
- Stránky projdou `test_design_system_contract.py` a
  `test_ui_layout_theme.py` (obě témata × oba layouty).
- `CLAUDE.md` generovaný pro workspace s aktivní vrstvou obsahuje sekci
  o autoritativnosti; bez modelů ji neobsahuje.

---

## 4. Fáze 2 — write-back: minimální užitečný krok (odpověď na Q4)

Zadání říká, že chybí „doslova dvě pole". Po ověření je to přesnější a méně
příznivé: **`connectors/keboola/semantic_ossie.py` nezachovává UUID objektů
ani jejich revizi vůbec.** Adaptér čte `m["id"]` jen pro modely
(ř. 381, kvůli fetchování položek) a `model_item.get("id")` jako fallback
jména (ř. 295). Pro dataset/metric/constraint/relationship/glossary se
skládá dokument bez upstream identity. Takže identita se neztrácí až v
`build_metric_row` — v dokumentové cestě tam nikdy nedorazí.

To je zároveň dobrá zpráva, protože to určuje, kam patří oprava: **do
adaptéru, ne do ploché tabulky.** Dokument je kánon a ukládá se verbatim, takže
identita zapsaná do `custom_extensions` přežije všechno ostatní.

**Minimální užitečný krok fáze 2** (jedna PR, žádný zápis nikam ven):

Adaptér přidá do `AGNES` extension každého objektu `{"metastore_id": <uuid>,
"metastore_revision": <meta>}`. Nic víc. Žádný write endpoint, žádné tlačítko.

Proč je to samo o sobě užitečné a ne jen příprava:
- Je to **jediný krok, který je nevratně ztrátový, když se odkládá.** Každý
  sync bez něj přepíše dokument bez identity; historie se nedá dopočítat.
- Odemyká *diff*, i bez zápisu: „tenhle objekt v Agnes se liší od revize R
  v Metastore" je použitelná informace pro admina hned, přes coverage report.
- Je testovatelný bez upstreamu — fixture Metastore odpovědi → dokument
  nese uuid a revizi.

Teprve druhý krok fáze 2 je vlastní zápis, a `expression` držené verbatim
z něj dělá malou věc: pro Keboola-importovanou metriku je změna popisu nebo
constraintu PATCH na `(uuid, revision)` bez rekonstrukce SQL. Ale ten krok
potřebuje rozhodnutí, které tenhle plán neotvírá — kdo vyhrává konflikt, když
se revize rozejde (viz otevřené otázky).

Doporučení: **udělat krok 1 hned ve vlně 1** (adaptér se v ní stejně otevírá
kvůli `AGNES` extension) a krok 2 nechat jako samostatnou fázi 2 po vlně 4.

---

## 5. Souhrn pořadí

| Vlna | Obsah | Mění data instalací? | Blokuje |
|---|---|---|---|
| 0 | N1, N2, `grain` default | ne (jen nové zápisy) | nic |
| 1 | Parita projektoru (C1) + N4 + identita objektů | ne | vlna 2 |
| 2 | Shadow → diff → cutover → smazat legacy (C2–C5) | **ano** | vlna 3 |
| 3 | `validate_semantic_query` na REST/CLI/MCP | ne | — |
| 4 | UI, čtecí trojice, skill, `CLAUDE.md` | ne | — |
| f2 | Write-back do Metastore | ne (zapisuje ven) | — |

Vlny 3 a 4 jsou po vlně 2 vzájemně nezávislé a dají se stavět paralelně
(`/agnes-build`); 3 před 4, pokud je kapacita jen na jednu, protože odemyká
hotový kód.

---

## 6. Otevřené otázky

Věci, které z repozitáře ověřit nejdou a mění rozhodnutí, ne jen detail.

**O1 — Závisí nějaká živá instalace na dnešních `metric_definitions.id`
tvaru `keboola/{uuid}/{name}`?** V repu žádná FK, RBAC vazba ani
`ResourceType` na metriky neexistuje, takže z pohledu kódu je změna id
bezpečná. Ale analytické workspaces mají `CLAUDE.local.md` a uložené session —
pokud si tam někdo id zapsal, cutover mu je rozbije. Odpověď mění C4:
buď „přijmout změnu + hint" (doporučeno), nebo kompatibilní alias vrstva.

**O2 — Je `semantic-metric.sql` z Metastore vždy holý agregační fragment?**
Adaptér i legacy composer to předpokládají (`compose_sql` fragment obalí).
Pokud upstream někdy pošle celý `SELECT`, obalení vyrobí nevalidní SQL. Nejde
ověřit z repa — jen z reálného projektu. Odpověď mění C1: buď stačí obalit,
nebo je potřeba detekce „už je to dotaz".

**O3 — Vystavuje Metastore API revizi objektu (`meta`) na `list_items`, nebo
až na detailu?** Zadání tvrdí, že `meta` existuje; adaptér ho nečte, takže
z kódu se to nepozná. Odpověď mění krok 1 fáze 2: jestli je revize v listu,
je to čistě aditivní změna; jestli až na detailu, přidává N+1 round-tripů na
sync a chce to jiný fetch.

**O4 — Kdo vyhrává, když se při write-backu revize rozejde?** Není to fakt
k ověření, ale rozhodnutí k udělání — a musí padnout před krokem 2 fáze 2, ne
během něj. Varianty: odmítnout zápis (409, jako optimistický zámek v parity
specu), nebo přepsat, nebo nabídnout merge. Konzistentní s parity specem by
bylo 409.
