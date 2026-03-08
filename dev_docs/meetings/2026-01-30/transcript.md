00:00:01 Padák
Tak, hele, první, co mě zaseklo, bylo, že já už to mám jako nainstalovaný, jako by mám uživatela jednoho, LabCone, Pavel a Jirka taky, a Matěj Kis tam vlastně upravil velmi správně teda to, že tam přidal ten Cloud MD, jenomže ten můj uživatel se nemá šanci k němu jako dostat ten, z pohledu toho usera, já tam pouštím sync data, to mi synkuje ty parkety, v životě se nedostanu k tomu, aby se stalo to, co my chceme na tom servru.

00:00:32 Padák
Takže my jsme změnili něco, jak to funguje, ale nejsme schopní to dostat těm lidem, protože se prostě synkují jenom data, nesynkují se ty skripty, dokumentace a tak. Takže i kdyby jsem si znova pustil skript, který mi vyrobí ten, jakoby to, co si my myslíme, že jsou ty instructions. což v té předchozí verze byl project.json a to bysme upravili, ať to dělá clod.md, tak ten user nemá update toho skriptu, prostě jo.

00:01:05 Padák
Samozřejmě to všechno funguje tak, že mu dáš jakoby ty rsync komandy ručně, ať si je pustí prostě znovu a tak.

00:01:13 Dasa
No a dobré, tak řešení tohle je to jen do manuálu clodový dat.

00:01:20 Padák
Řešení je, že ten sync data synkuje, není to sync data, synkuje to všechno ze serveru.

00:01:27 Speaker 3
Ano, to je to určitě.

00:01:30 Padák
Tím clod.md vznikl problém, jakmile začnu synkovat clod.md, který potřebuji při každém stažení dat jako vyrobit znova vlastně, protože jsme do něj my mohli dát nový instrukce, tak začne být problém, že co když jsi do něj dal něco user. který třeba by opakovaně dělal analýzu PSD-ovou plade, pak chtěl by se tam dát instrukce, jak to zrychlit, zjednodušit a tak. A my mu ho při každém synku přepíšeme. Tak to byl druhý problém, který jsem řešil. A pak jsem udělal ty notifikace a začal jsem mít,

00:02:01 Padák
uživat lokálně v notifikacích nějaký Python skript, to ti hnedka ukážu za chviličku. A ten potřebuji zase dostat na server, takže jsem potřeboval zase, všechny ty změny byly kolem toho synkovýho skriptu. Takže jsem potřeboval si upravit ten sync skript a synkovat věci od uživatele ven. Ale jakmile jsem začal synkovat věci od uživatele ven, tak jsem si všimnul, že v homu adresář skript vede na data user skript,

00:02:32 Padák
ale není tam nikde, jako co to je za user. A já měl jako, kdybych řekl, že user scriptem je teďka ten můj Python, který dělá tu notifikaci, mám jeden example, který vyrobí nový PSUGO projekty za 10 dní, den po dní a pošle graf a tabulku s datama. Tak to nahraju a najednou to budou mít úplně všichni uživatelé, což v tu chvíli já nechci tohle udělat. A do toho já už tím, že jsem to nějakou chvíli používal, tak na mém disku byly hromady nějakých dat a HTML reportů, v kterých bylo něco napsané.

00:03:15 Padák
A teďka, když jsem řekl něco Claudovi, tak jsem si všiml, že on iniciativně vlezl do folderu Scripts a začal tam něco chtít modifikovat, což je ten náš folder, který je na servru. Čili kdyby nám nezmodifikoval Sync Script, tak by to bylo pro nás OK, protože my si v dalším updateu ty změny přepíšeme, takže kdyby nám něco rozbíjel, tak si to opravíme, ale zase user by byl nasraný. A z tohohle jsem si řekl, hej, to je prostě vlastně jako dvě nedomyšlení, ta struku.

00:03:45 Dasa
Dobre, hovor já jim a potom mám myšlenku, ale aby jsi neodbíhal daleko, tak mi tady potom...

00:03:50 Padák
Důkvěci, nebo už jsem vlastně hotový, takže tohle byla jako oblast, kterou jsem pokryl a udělal jsem v zásadě to, že jsem řekl z pohledu uživatele, a teďka jdu tady už to tady nazdílet. Sedíš u kompu nebo máš k tomu externí monitor. 

00:04:08 Speaker 3
Já jsem na externím a podle toho.

00:04:13 Padák
nasdílím celý desktop, anebo jenom to okno. 

00:04:15 Dasa
Hej, ne, ne, jsem normálně jsem na jiba notebooku a... Super, vůbec nevadí. Náročné je připáhnout káble.

00:04:25 Padák
Takže tohle je můj, tohle to je můj jako home a tohle je můj home na servru a tady vlevo to je můj desktopovej jako lokální.

00:04:44 Dasa
co ti tam vytvoril.

00:04:47 Padák
Takže mám CloudMD a mám jako server, kde jsou naše věci, Docs, Example, Metadata, Parked, Scripts, tam je přesně to, co bys jako čekala, jo. A pak je User a v něm je Artifacts, DuckDB, Notifications, Parked a Scripts. A teďka, co je co? Tohle jsou naše věci, ty všechny známe a z těch našich věcí zmizely DuckDB. Důvod je, že ho vyrábím na klientovi, tak není ze serveru synkovany, tak je tady, jo.

00:05:18 Dasa
To já tam rozumím.

00:05:20 Padák
Takže tohle je nezajímavé. A v Useru, artefakty jsou prostě věci, co si ten uživatel vyrábí za pochodu. Já to dřív měl u sebe pojmenu jako reports. Kteroukoliv z těchto věcí, když odevřu, tak to hodí nějaký dashboard, vlastně to dělá, jo.

00:05:38 Dasa
Dobre. Možná mi ještě povedz, nejakým způsobem si řešil jíba to, jak se to do těchto artefaktů dostává, okrem toho, že mu to poví. Uvířit. To má přesně tam uložit. 

00:05:48 Padák
Jenom tím, no.

00:05:49 Dasa
Jo, dobré, OK.

00:05:50 Padák
A teďka já si uklidil tu svou starou složku a ten bordel jsem si dal do Artifacts a v tom Cloud MD to musíme dát v dobrý instrukci. A obsah Cloud MD jsem aktivně neřešil teďka, ten je Matěj Kisův. DugDB je ta DugDB, která vzniká s kryptama při inicializaci. Do notifikací se dávají soubory, které dělají ty notifikace, teď ti ukážu. A v parketech tam nic nemám, to by byly prostě, kdybych si já začal.

00:06:21 Padák
vyrábět nějaké předpočítání. Pavel si postavil appku, která nad těma datama jako si ukazuje pricing kalkulačků. A teď by jí vlastně postupně chtěl dát klientům. Neřeším update dat. Prostě jenom jako naprdal si na ty děto a kdyby si on udělal nějakou interně nějakou. přetransformující pipeline, tak by to měl jako třeba, tady by mohl nějaký data prostě. A z krypty by byl cokoliv, co si ten uživatel sám vymyslí, že mu něco zrychle zlepšuje.

00:06:52 Padák
Že to je strana uživatele a analogicky na tom serveru je teda vidět, jako v serveru je parita úplně k tomu, server folderu, když tam je docs, examples, metadata, data, parket, scripts a to vede na náš data folder a tam je to pořád identický. A když se podívám na users, tak to fyzicky leží v tom homu a nikam to neodchází, to je prostě v něm. A adresář users.

00:07:22 Padák
na tom mém homu, tak je prostě přesná kopie tohohle adresáře. Takže Users Notifications obsahuje prostě ten kriptíček na notifikace a upravený vlastně největší zásahy jsem pak udělal teda do ECOV. Všech je nějaká změna, protože se změnily ty testy a SyncData dělá to, že, to jako nechci prolajzat asi, ale vlastně má to, proto Syncuje to z User Folder Syncuje jako na server a Server Folder Syncuje ze serveru, jo.

00:08:03 Speaker 3
Dobré.

00:08:04 Padák
Nedělá to zatím nikdy synchronizaci ze serveru ke mně těch User Folderů. To je prostě jenom proto, aby na serveru to bylo, kdyby se s tím něco jako mělo dít, jo. Jasně. A teďka, takže to je tohle, teďka to odzdílim, ještě jsem hned, nazdílim Browser. Dobré.

00:08:30 Speaker 3
Co se vám tady přestalo? To nefunguje hodně, to je prostě strašné, ale... Dobře, tak prosím, na co si čítejte, jo. 

00:08:38 Padák
Připoji na KOL a můžem s nima si povídat.

00:08:42 Dasa
Tak já ji ho teď nevyháněm, len nechcem, aby tady někdo mi přišel teda s telkou a tak, že by mi to bylo zle po hladi.

00:08:50 Padák
Hele, a pak jsem šel, udělal jsem malou změnu tady, neobsahovou, ale redesignoval jsem ten dashboard, aby se nescrollovalo, nebylo to pod sebou, a tahleta věc se dynamicky generuje, když se pustí update z Kebuly. A je tam napsaný, kdy je to vyrobený, teda to. A to stejný pak je v tomhletom, a jako když tam přidáš novou tabulku, tak by tady měla být vidět, když ne, tak jakoby má to jasný místo, kde se to jako upravuje.

00:09:21 Padák
Je tam prostě last sync, compressed, uncompressed, a je tady telegram notifications, jo. Ten funguje tak, že user to vidí takhle unlinked, jo. Já teďka budu muset jít. Já se odevřu komp a nazdílim screen toho kompu.

00:09:44 Speaker 4
Teď jsem mě neslyšel.

00:09:46 Dasa
Dobře, pokračuj dál.

00:09:51 Padák
Počkej, já jsem chtěl. Já jdu trošku přeorganizovat, mám si okna na kompu, aby to bylo snesitelný a topnu sharing a dám tam celou screenu a to bude můj počítač. Teď jsem zpátky tady a ještě se hodím tohle. Takže teďka odtáhnu ten pop-up tak. Takže teďka... To mi to říká. Linky, Telegram, když to bude na Slacku, tak nemusíš nic linkovat,

00:10:26 Padák
protože Slack je autorizovaný tím kebulem účtem, stejně to můžeme jednodušší, ale já jako sobecky si to ladím na tomhle, protože se to používá pro mě tisíckrát líp.

00:10:36 Dasa
Určitě.

00:10:38 Padák
Takže Telegram teďka máme, já ho klírnu, moment. Tak, že tady mám jako toho kebula bota, já to stahnu, jako že kebula data bot jsem nazval nějakou věc, čili kdybych tam na ní nebyl, tak tady dám prostě vyhledat kebula data bot a s ním si můžu povídat, jo.

00:11:08 Padák
Teďka já mu řeknu start. Co on udělá, že mi vygnuje číslo, když mu řeknu, má nějaký komandy, jo, takže když mu řeknu, kdo jsem, tak on jako nejde. Takže ví. Takže ještě teda, promiň, ještě tady odevřu Telegram a vlezu na ten server a na něm data, notifications a tu tam pending kody.

00:11:38 Padák
Je to úplně strašně jako jednoduchý, takže tohle je ten kód, tady ta 60, co se vyrobilo, jo, omelem, jakoby omelem, no prostě. A má nějakou platnost, jo. A on vlastně mi dá kód a ví, v jakém IDčku toho chatu ten kód je udělený a teďka podle toho, do kterého zalogovaného okna na tom webu dám ten kód, tak on zjistí, kdo jsem. Tak to je jako jednoduchý, je to, jo. Takže já když tady řeknu start, tak on mi dá nějaký kód, fakticky teda tady se zvětšil, se přepsal ten skript, protože...

00:12:17 Padák
jakoby ví, že je novej kód ve stejný konverzaci, on má jako 100 telegram mapy. A teďka, když ten kód strčím semdle, verifaj, tak on mě verifikuje a napíše na tom telegramu vítej, ale nenapsal vítej, tak to je blbě, to jsem by měl dělat, ale a tady, když dám help, dám, kdo jsem, tak už ví, že jsem Petr, Petr Kevula com, tady se ty pending kódy.

00:12:48 Padák
smazali, už mají jako 2 byty a oproti tomu si všimněte, telegram users má 2 byty a tady, jako když dám telegram users, tak už ví, že je uživatel Petr, tohle username na tom serveru je v tomhle tom chat IDčku a tady to bylo linknutý. V tuhle tu chvíli, co on umí, sorry, takhle, co on umí je, tak mě můžu to znovu initnout, tak když bych teďka dal start, tak mi doufám, řekni, že jsem už slinkovaný, tak to udělal. Mám tam test, což je to, co jsi viděl, to on pustí testovací report.

00:13:20 Padák
a nableje do toho prostě nějaký text a obrázek, jo. A když tam dám.

00:13:27 Dasa
A teda test to znamená, že ti ve svojí skripto, které jsi měl. 

00:13:32 Padák
tak ti se spustí. Testuje spojení jenom, nic to, jakoby ta věc je pro všechny stejná, jenom je to jako ze serveru umí poslat data, jakoby on initnout udělá ten loop, jakoby v tom. A pak tady mám udělaný status a ten mi vypíše, se otevřu ten terminál a tady to leave a dám tam petrkits a ten mi vypíše vlastně, všechny věci, co jsou v user notifications, jo, tak tam píše ten,

00:14:03 Padák
PAG Daily Report a přidává tady tlačítko a tím, když ho zmáčknu, tak on ten report exekne a pošle mi výsledky z něho. To, co poslal teďka, jsem si přednastavil. Jakoby to jsem udělal v kódu já, prostě to, že tam je ten graf a že vypadá takhle a jaká je ta bůlka a že zrovna koukáme česky. To je v tom skriptu, myslím v tom PAG Daily Report.

00:14:32 Speaker 3
Ano, to se tam zpěstilo.

00:14:33 Padák
A ten se jako spustil, přesně tak. V praxi to pak vypadá tak a to mi jako by nepřišlo teď ráno zároveň, takže to budu muset jako zdebagovat, že tadyhle jsem teda jenom pro zopakování, Who am I, takže tam jsem v User Petr a v Korontabu je vidět, že ten User Petr pošle v 7.30 a to se jako by neproběhlo, tak to si zdebaguju v evropský timezóně. tak to Pythonem z virtuálního environmentu, který mi musel Klod nastavit,

00:15:07 Padák
což je druhá zmínka, při initu virtuálního environmentu lokálně i Klod je instruovaný, aby to stejný udělal na tom servru pod tím uživatelem, protože potřeba mít ty prostředí identické. A aby se o to jako staral, takže když doinstalujete nějaký balíček lokálně, protože bych si třeba řekl, že chci animovaný GIF jako vyrábět, tak ho potřebuji mít na tom servru samozřejmě. Takže to tím Pythonem z toho venvu zavolá nějakej NotifyRunner.

00:15:38 Padák
a ten někam pak loguje. A NotifyRunner je spuštěný pod uživatelem Petr, a tak jde a v tom jeho homu on se sučkne jako na něj, ne sučkne, protože je pod ním spuštěný, takže on běží pod Petrem a v mém homu najde všechny ty skripty a spustí je. A je tam jedna věc, kvůli které to neproběhlo, já jako vlastně vím, proč se to nestalo. Teď jsem si to uvědomil. A to nevím moc, co s tím je, to je design toho systému notifikací.

00:16:09 Padák
Teď tady je folder .notifications, jo. A v tom folderu .notifications jsou logi a state. A v tom state je pgDailyReport, jo. Takže když dám notification state pgDailyReport, tak vidím, jakdy to naposledy bylo poslané. A ten skript samotný v sobě má cooldown period, což jsem si vymyslel. To je jakoby možnost, to jsem řekl. V systému těch notifikací dělají třeba.

00:16:42 Padák
tu cooldown periodu. Způsob je takovej, že user by mohl, důvod je ten, že user by mohl říct. Hele, mě zajímá, představme si teďka na chvilku, že ty data jsou live, jo. A on by řekl, Anička, řekni. Jakmile se vysere na nově konektor do jejich CRMka, tak já bych to chtěla vědět. A řekne si, udělá si desetiminutový ček, deplojíme to na server, a ta věc ale nemá co deset minut posílat notifikace,

00:17:13 Padák
nestalo se to, nestalo se to, nebo empty věci, ta je má poslat jen, a jakoby chceš to testovat pořád, ale pokud by se to vysralo, a tak ti pošle notifikaci, a za deset minut by ti poslal zase to, zase je to vysrané, je to vlastně pořád, nejim to sere. Takže jakoby je to na to, aby se, je to peak of mechanismu, aby se uklidnil, uklidnil ten systém, takže teďka jakoby si myslím, že když půjdu a pustím tato, jakoby dám notify runner,

00:17:46 Padák
ne, to tady, proč mi to nedošeptává, tak on dal, je v cooldownu, a to je důvod, proč mi to nepřišlo. A když ten... Notification state page smažu, a teď jakoby probíhá ten crontab, tak vlevo naskočil do toho telegramu, ty data přiběhly.

00:18:13 Dasa
Jasně. Mě to dojde, že tímto já rozumím tvůj use case, a je to presně to, kdy máme v Datadoku nastavené notifikáci, a nechceš, aby ti to prostě stále posílalo, že ani za dvě hodiny ti nedešel ten success job. Ale není to momentálně při tento jako pilotě trošičku komplikácia. 

00:18:34 Padák
Hele, pro mě ne, protože já jsem řekl, kolo tmy to vyrobil, víš, já můžu jít s díl, dělejme notifikace bez cooldown perioditu. Věc z té cooldown periody si dělá ten report. On říká, jaká je a že je denní třeba, jo. Takže já jsem jako, hele, svým způsobem máš pravdu, že to je jakoby problém, ale já se nechci, dostat do situace. My jsme jakoby, kdyby jsme... Společně, než jste vyšli udělat tu první verzi,

00:19:08 Padák
kdyby jsme si sedli a ještě hodinku si o tom povídali a vymysleli jsme, že tam vlastně budou ty synky od usera na server. My jsme říkali, něco by mohl dělat na serveru, ale spíš to nechceme. A neměli jsme ten use case, ale jakoby třeba bychom to nevymysleli. Kdyby jsme na to přišli, tak bychom rovnou nesetupovali. strukturu adresářů jinak. A vlastně teď je to v situaci, že když cokoliv dojebeme jinak, tak ten sync script máš prostě v kompu toho useru máš install script, který můžeš na dálku řešit, co dělá.

00:19:43 Padák
A vlastně při prvním následujícím zpuštěním ta stará verze update-ne i ten sync script. A ona může přiníst třeba nějaký migration script a v tom novým sync scriptu zároveň vědomí, že má pouštět migration script a když mu to háže erory, ať si pustí nějaký fallback předchozí. Teď to jde, protože tam máme cestu. No a já vlastně tím, že jsem jako řekl, hele existuje možnost cooldown periody třeba, tak vlastně jakoby otvírám cestu, že to budeme na tom stavět, ale jako používat to vůbec nemusíš.

00:20:12 Speaker 3
Jasně.

00:20:13 Padák
Nicméně tohle je jakoby setup, co jsem tam jako udělal teďka.

00:20:17 Dasa
Já mám hej pár dotazů, na které technické, na které jíba jako připomínky. No asi to bude takové nějaké zmatené, nevadí. Když začneme od konca. Tak ty jsi hovoril, že jak si nějaký člověk právě vymyslí nějaký skript, dá si ho jako, aby mu z toho vznikaly notifikace a podobně, tak se mu tam samozřejmě musí prenést jeho ENV, alebo teda musí se jeho virtuálné prostředí tam prenést.

00:20:50 Dasa
Já tam úplně nevím, jak to na ten servri funguje, protože jestli tam reálně zinstaluješ requirements, tak je to aj tak prdlné a to jsem tam nikde neviděla.

00:20:59 Padák
Ale funguje to vlastně jako jednoduše, jo, ten, dobrý dotaz, jo, třeba si vypnu tady tuto hru, jsou nasprojovanou ještě. Funguje to tak, že něco vytvoří ten lokální ENV.

00:21:22 Speaker 3
Ano.

00:21:24 Padák
Už ta stejná věc se musí odehrát jako remote.

00:21:28 Dasa
Ano, ale lokální EMF se vytváří tak, že se nainstalují balíky a ty tam potom běží. Jakékoliv, když si vymyslíš nějakou novou knihovou...

00:21:41 Padák
Ten remote je úplně stejný, jenom je tam předtím SSH, kids, vozovky a ta stejná komanda, co se odehrá na lokále, se odehrá na tom remote. Dobře. Počkej, tohle je dobré, něco ti je divné na tom.

00:22:09 Dasa
Ano, něco mi na tom je divné, ale nevím co. Že já si to teraz v hlavě snažím vlastně uvědomit, jak to probíhá. Ale za normálných okolností ENF máš jako list balíků, které k nečemu potrebuješ, aby si vytvorilo to svoje prostředí.

00:22:43 Dasa
Mě tam stále já nechápem tého, kdy se to k tebě nainštaluje. Kdy se to na servery nainštaluje do tvojeho home, aby to běželo.

00:22:56 Padák
Ale my máme, já to musím jako najít teďka, ale v tom, já teď se vrtám v té, my máme nějaký skript, který se jmenuje Activate ENF, tak v něm to je jako, a tam je napsaný tohle. Takže v těch prvních instrukcích je, že se má spustit. Jo, a tohle to udělá ten lokální.

00:23:30 Padák
Je to ale, tohle ho aktivuje, to není ono.

00:23:38 Dasa
Tady tento Ketáš update, tak podle mě tam by to, ale tam se aspoň dozvíme, na co nás to odkládá. Kurňa, tady máme co, tady máme, scrollu, už to bylo zase, iba activate.

00:23:54 Padák
Tady já hledám Python minus mvenv jako komand, kterým se to vyrobilo, buď to vyrábí náš skript, anebo to, to může být ten init tady, jo. A tady, checking Python, čekne Python, jo, bezva a tady řekne, když, takže, když, tady vytváří ten mvenv, takže když není, a tady má být tečka zase, jo, tady je, pořád je tady.

00:24:22 Dasa
Jo, jo, dobře, tam měl to, asi teda máme to stáhnuté, jo.

00:24:29 Padák
Já to mám fresh, no, tohle to bude podle mě, počkej.

00:24:33 Dasa
Jo, jo, v pohodě, to já se na to potom můžu kouknout.

00:24:37 Padák
Tak je Bula, Internal Data Analyst, Crypt.

00:24:44 Speaker 3
Init, to bylo, no.

00:24:47 Padák
Init, tady je pořád jako ven, bez tečky.

00:24:50 Speaker 4
Jo, jo, to nevadí, jo, ale tak v tomhletom, tady, počkej, tady jsou, tady.

00:25:08 Padák
Jo, tohle, tohle, ale není, já to neskoumám nikdy, ty vaše skripty, takže se v nich jako úplně nevyznám.

00:25:21 Dasa
Potřebujeme trošičku pomalší scrollování, já si to taky otevím.

00:25:34 Padák
Ale je to potřeba vyřešit a říct mu, že to má dělat, jinak jako kde se, tohle nemám podchycené a dočištěné, ale stane se to tak, že při nasazení té notifikace on zjistí, že mu neběží a doinstaluje si to tam.

00:25:49 Dasa
Jo, my tady přímo v té initě máš potom pip install requirements a podobné takové hovadiny. To tam jako proběhne, je pravda, že to doví do jakého folderu toto. Dobře, ten folder tam dám. A to je ta důležitá věc na tem, ne? No, jako je, ale...

00:26:12 Padák
Jakoby, pojím tam, já mám na tom serveru, teďka mám neinstalený venv, protože tam mám ten skript a on potřebuje nějaký balíky, tak je tam udělaný, protože na ten server to neinstaloval zase ten můj cloud, nebo neinstaloval, to jenom skopíroval AirSynkem, ale vyskoušel si to tam a zjistil, že nemá, a zjistil, že nemá Httpxl knihovnu třeba, jo, tak tam dodělal prostě ty věci, všechno, co bylo potřeba.

00:26:42 Dasa
Jo, jo, hej, dobré, já se v tento asi ještě pohrabím, já to potřebujem nějak, a ještě až zravidujem tyto...

00:26:51 Padák
Čekaj, čekaj, moment, ještě mi dej sekundu, jestli to je možná... Ne, to je vlastně to samý, podle. No a tohle potřebuje, a tohle CloudMD jsem proles, možná, možná, moment, já to totiž ale někam podchytil si, nebo žil jsem v tom.

00:27:21 Padák
Dál bych to, hele, mělo, vlastně stačí to napsat do CloudMD, jako zároveň za mě, ale není tam o tom servru vlastně uvedený nic, no. Tak start.

00:27:58 Padák
Blablabla. On je osný a ví, kudy na to, že jo. Takže ti umí navíc, že se máš nalinkovat. A ví, že jsou v User Notifications ty věci, že musí být. A že tam je nějakej runner. A že tady je Server Docs Notification MD jako Full Guide. Takže v Docs Notifications MD. A tady má napsaný tohle. A možná v tomhle bude napsaný o tom venru. Jak se vyrobí notifikační skript.

00:28:29 Padák
Tady je ta cooldown perioda popsaná. Že si nastaví crontab. Možný cooldown values. Já to tam doplním do toho ještě.

00:28:45 Dasa
Hej, v pohodě. Já se zaměrám aj tak jako na to, jak to my to máme. Jak my to tam inicializujeme. Alebo jak se to potom prenese ke klientovi a přesně tam ještě ty, tečky před WMV dám. Další věc, která mě napadla, když jsi hovoril, že, přírodně, když pracuješ na lokáli, tak Cloud chce ti například šahat do skriptů,

00:29:15 Dasa
které jsou naše serverové a jedno s druhým. Neděšilo by tohleto, kdyby jsme do Clodovi, do těch jeho růz dali jako odmítnutí. Ne, jako mě to on by nemohl, nejsou jako permissions také, že nemůže zapisovat jíba do těch určitých zložek.

00:29:37 Padák
No, ale on by, on si spustí ten skript, který dělá ten sync.

00:29:45 Dasa
Jo, ale já jsem myslela, že v rámci toho si stáhne i Cloud MD. A tak, jak ty můžeš mít na svém lokálním Cloud MDV jako v projekti, kde mu napíšeš, že nemůžeš koukat do EMVU, tak či neexistuje něco takového, že můžeš koukat, ale nemůžeš vypřepisovat, že má jen read-only práva na nějaké zložky.

00:30:13 Padák
Jo, vím, co myslíš, ale počkej, já to tady odevřu. Tady to bude napsané podle mě už. Tady je directory structure, tady je řečený server read-only. Read-only, sync form server, never modify. Tohle by mělo stačit. S tím bych to vyzkoušel. Když se ukáže, že to nestačí, tak to budeme řešit jinak.

00:30:41 Dasa
Jo, já jsem to brala tak, že je na to ta taková presná štruktúra o tom, že dáš věci do dinej a prostě tam ti to absolutně je safe, že máš nějaký env v souboru. Dobre, určitě, můžeme to vyzkoušet.

00:30:58 Padák
A to funguje jak tady to. 

00:30:59 Dasa
Tak to já mám. Počkej, já se podívám, jak já to mám nastavené u sebe. Podle mě do MDčka máš jako klučové slova, které mu dáš a ty jsou zprávně vysvaté. Tohle je text a máš ho tam hromadu.

00:31:14 Padák
Jo, takhle, ale to jakoby, já myslel, jestli existuje nějaký clot setting, který by říkal, nikdy nepiš sem, že by mu zakazal. Můžeme jako do permissions to udělat, no. 

00:31:25 Dasa
No, jo, tak jsem to myslela, ty permissions, to, že teď máme nebo používáme zvětšení permissions na to, aby nekoukal, tak možná existuje jako permission na...

00:31:39 Padák
Jo, to existuje, to je tohle.

00:31:41 Dasa
Ale to je, že nemůže vůbec koukat, já jsem myslela, že bude jen takový...

00:31:45 Padák
No ne, on má tady třeba ty, tam má jako, já se rychle půjdu na perplexity. Existuje cloud permissions, aby nikdy nepřepisalo...

00:32:02 Dasa
To nebylo v CloudMD, to bylo v settingu, no vidíš to.

00:32:05 Padák
Ale to musí být, to CloudMD je prostě jenom text, takže to je to, jakoby můžeme jinak frázovat my, never modify, ale...

00:32:15 Dasa
Jo, jo, já jsem si to iba popletla, že to je v CloudMD a ono je to fakt v settingu. Ano, tohle jsem myslela. Permissions.

00:32:23 Padák
Jo, má tam edit. Je tam, existuje to, no. To je hodně dobrá poznámka. Takže počkej. Já to schválně tady vyzkouším, jo. Permissions. Jenže tady není, tady je allow, ask, deny a workspace. Hybej do cloud settings. Jo, počkej, ale, jo, zakáže editace, jo. Zakaz editace server folderů a všeho pod ním.

00:33:14 Padák
ale musíš mít právo se do toho koukat libovolně, ale nikdy tam nepíš nic. Já se ještě vrátím k tomu tady, co pak existuje, a to je důležité, a jsem to vůbec nevěděl, ale zjistil jsem to při tom, když jsem s nima to včera ladil, je, že existuje cloud local MD, a ten my nepřepisujeme zase,

00:33:45 Padák
takže user, když by si chtěl uložit nějaký instrukce, tak si říká cloud local MD, your personal customizations, never overwritten, ale cloud MD je auto-updated from server. Čili jakoby tímhle by mělo být vyřešený, že my do Cloud MD strkáme instrukce ze servru a do Cloud Local tam si user může dávat, co chce. A já tady, hle, já tady udělám takovou věc jenom, že, já jsem včera, to udělám rychle, jenom ať na ty věci nezapomeňte,

00:34:20 Padák
v sobě jsem si udělal gydapíšu na nějakou úpravu toho ještě, jak funguje ty tlačítka v Telegramu na posílání těch notifikací, protože on jako ten Telegram bot neumí se sučknout na user a strašně jsem tam povypínal jako file system permissions, protože ten můj skript, ten obrázek udělá v tempu a ten file má sticky bit, tam je v tom ls a je tečko a ten dělá to, že když do toho tempu lidi zapisujou, tak nevidí si svoje věci navzájem. Mhm.

00:34:52 Padák
A takže jsem to musel pohackovat, tak mám na to tedy vzdělánou opravu, tak do ní jsem si dal řečka v komentáři, ať fixnu ten venv ještě.

00:35:00 Dasa
A do tohohle...

00:35:02 Padák
To je tohle druhý. Ne, počkej, to jsou mý notifikace. A teďka, tady pak jsem udělal věc tohleto githubišů. Když se z kebuli synknou data do toho a vyrobí se ty parketfajly, tak se všem vyrobí všechny znova.

00:35:23 Speaker 3
To by neměli.

00:35:27 Padák
To se, jakoby, nemusí se vyrábět znova, ale změnil se jim mtime, modification time. A rsync, kdykoliv jsi ho pustila, tak synknul všechno. Protože čekoval mtime plus velikost souboru. A já jsem tam přidal parametr, který je minus minus checksum. což je tahle ta věc tady dole v komentáři. Takže teďka ten synkovací skryt, který je zde,

00:36:01 Padák
SyncData, tak má tahle ten checksum. Tím se to jako vyřešilo úplně a funguje to perfektně. Synk nejenom změny, ale ty změny pozná tak, že jak na lokále, tak na tom servru dělá checksum i těch soubudů, že musí celý projít. Něco jako MD5, kdyby jsi počítal. A tím pádem každý ten synk jako poměrně dost zatěžuje ten nebo... Pro pet uživatelů ne, ale kdyby jich tam bylo moc a pouštěli to často, tak zatím už je server a na tom lokále dělá taky I.O.

00:36:33 Padák
A je to, to GIDA píšuje na to, zkontrolovat, co se reálně děje při tom, když se updatují data z Kebuli.

00:36:41 Dasa
Hej, dobré, ale na toto to určitě jde na mě. Já jako checksum...

00:36:47 Padák
No to jsem ti to přiřadil i tady, já ti to jako bych chtěl teďka jenom říct, že jsem to udělal. Našel jsem tam nějakou věc, fixnul jsem ji tím checksumem a udělal jsem na ní GIDA píšu, tam jsem to popsal. A teď jsem chtěl říct do toho, přidat jednu věc. Já to sem dám jako do komentu a nesouvisí to s tím. A tohle je věc, kterou bych nechal jako na tebe, ať se o to dělíme. A ta by byla pořešit, aby sync data z localhostu,

00:37:17 Padák
tím budu myslet ten můj komp. Synknul i Cloud Local MD, na to jsem ji zapomněl. Ať když user přijde o komp, má na serveru všechno. Tak to je jenom takový rychlý node k tomu. A to bude... Ten by tam mohl ležet v tom homu toho useru klidně.

00:37:48 Speaker 3
Jasně, jasně.

00:37:49 Padák
A já potom udělám to, o to se dopostarám. Já mám tady připravený... Tady mám připravený research na backup toho serveru, jo. Jako backup and disaster recovery. Který v zásadě spočívá v tom, že když by ten server krešnul celý, tak my přijdeme... Já zase tady odevřu. Teďka jsem u sebe. Teď jsem na serveru, teď jsem tam root.

00:38:25 Padák
To není přehledný úplně, tak počkej, mount. Tak takhle, tohle bude, stačí tohle. Tak vlastně tam je 10-gigovej disk, který je namountovaný do rootu. A ten udělá Google s tím VM-kem. A pak je tam druhý disk, který má 30 giga a ten je na to lomeno data. Pokud by se něco stalo a ten server by nám někdo smazal, krešnul, protože jsme se na něj už nepřipojili, neobnovili to, tak jako přijdeme i o ty homy uživatelský teďka.

00:38:58 Padák
A já to celý předělám tak, aby i ty user homy ležely na tom SDB disku. A nebo udělám novej disk pro ně. Asi udělám novej disk, že tam bude sdc a na něm budou homy. A ty disky pak Google snapshotuje. Znamená, že co je na těch diskách, tak máš někde v nějakém jejich backupu. A ten je třeba desetidenní a můžeš si nahodit server znova. A mám v tom researchovém tásku tady, je detailně prolezený.

00:39:29 Padák
Vlastně náš deploy script všechno recreate, takže třeba ty sudoers, kterýma se řeší, kdo se jak tam může a práva. To všechno je v tom deployment scriptu. Takže my vlastně všechnu knowledge máme v GitHubu. v tom repu, včetně těch secretů. Ale něco, co se vyrábí na tom serveru, bude jedinečný a to je třeba ten klod LocalMD. Že kdyby si teďka Jirka Maňaz do toho nadampoval nějaký znalosti a ukradl jmu počítač, tak bude říkat, no to je fakt dobrý, ty vlekčou mi to je, já to musím si vymýšlet.

00:40:00 Padák
znova, protože vlastně o to přijde. Takže, já jakoby je to jedno z témat a zároveň platí, a to je jakoby vidět tady tomu, df-h, že máme jenom tři giga volného místa na tom na té rootové partičně a ty homy vyrábí ty virtuální environmenty, který mají pár set mega a když to každej udělá, tak každej bude mít giga, třeba jakoby, jo. Tak proto je potřeba to.

00:40:30 Padák
přešachovat a s tím počítám a mám to tady tímhle připravený.

00:40:34 Speaker 3
Určitě, určitě.

00:40:36 Dasa
Dobrý den, mě se následují představující skripty, já se budu muset podívat, co přesně dělají, protože já jsem na serveri párkrát upravovala nějaké práva na zložkách a já vlastně nevím, či by to měl dělat i ten deploy script, takže já to tak do něho doplním, že jsem si tady uvedla, nech si to tam, kde jsem to já ručně přetvárala. A jinak to vlastně nevím, či to může nějak s tím souviset, ale když vztahujeme data z Kevuly, tak k tomu byl napojený i script na sync metadata jako timestampy toho, když se to udělá.

00:41:22 Dasa
Ano. A tento měl být nazdílený na lokál a tam se mělo něco porovnat, či teda jako jak jsme na tem. Je to moc hezké, no teda. Já to chci říct. Už nabíhal 2000 kroků. Jeme tady v ovýváku.

00:41:38 Padák
Tak bysme mohli porovnat toto, já mám postudnejch 230.

00:41:49 Dasa
No tak, ale on tady fakt jako to, tady mi tady běhajou. Ježiš, no a tento skript. No ano, já jsem se koukala potom do logu, co si nechávám při každém kronběhu generovat. A tam nebylo nastavené permission, to znamená, ten skript sice preběhal, co měl by generovat metadata, ale neměl právo zápisu, to znamená, nikdy žádné nebyly.

00:42:15 Padák
A k čemu je potřebujeme ty metadata na straně toho usera? Já jsem řekl, jako všim toho foldru metadata, který je v tom.

00:42:24 Dasa
Nechala jsem si to vysvětlit iba Clodem. A ten mi poveděl, že je to o tom, že potom ten uživatel vlastně ví, či potřebuje synchronizovat. A nejsem si jistá, či to tam je potom dané, že když to nemáš, tak prosím vás, neběhajte to líp, alebo někde jinde.

00:42:45 Padák
No, tohle je jako samozřejmě zajímavé. Otázka je, tady je jakoby, Claude M. D. má tohle popis, jo. 

00:42:53 Dasa
Jo, jo, no, Czech Freshness, jo, ale myslím, že Czech Freshness, Freshness v Ukrajině a tyto věci, pro mě je to strašně zložité vyslovovat, tam má někde kontrolování právě sůboru .metadata, whatever, JSON, a ten tam určitě nebýval.

00:43:19 Padák
Jo, hele, takže rozumím.

00:43:25 Dasa
Počkej, toto zapadá podle mě do toho, aby jsem zkontrolovala, aby se vždycky stahovali. Děti, prosím vás, můžete mluvit jinde, jo. 

00:43:32 Padák
Můj návrh je to následující, jo? Tady jsme, mě to vždycky jako štval ten check-freeze, už teda, já jsem se vždycky pustil sync metadata, on mi to synknul, trvalo to dlouho, protože to kopíroval znova, a pak mi to napsalo, pak to v tom cloudu udělalo rudě, protože to exit byl nebyl nula, jako na konci toho skriptu, protože nějaký metadata check byl, že to je starý ty data.

00:44:04 Padák
A psal mi to ten...

00:44:05 Dasa
To bylo v tom případě, ten metadata check byl starý, protože neměli jsme soubor metadata.

00:44:12 Padák
Jo, a teď mi ale...

00:44:18 Speaker 3
Chceš vědět, co to přesně volá. 

00:44:19 Padák
No já, počkej, já teď jako chci říct, že jakoby ve servru se metadata stahnou jenom tím, že se udělal AirSync, jo? Podle mě.

00:44:29 Dasa
ano. No počkaj.

00:44:31 Padák
No takže. Kdyby se tam mělo přijít.

00:44:33 Dasa
check freshness, to by mělo běžet jak první, podle mě.

00:44:38 Padák
Tak já to tady proskenovávám očima. a nikde tu nevidím jako stažení z toho servru. Je tady jediný slovo AirSync v komentáři.

00:45:09 Padák
To znamená, že ta věc porovnává metadata, který se stáhne při tom, když proběhne stažení dát, že jo? Jakoby ten AirSync si testuje ty metadata, on pozná, že to je, jako on stahne prostě, když budeme, asi mi přijde dobrý, že na začátku máme ty data, jako že jsou tak, jak jsou, že to nejsou stovky souborů třeba, jo, ale pojím tam mít jako hodinový granulát, což bylo v tý mý prvním myšlence, je o tom, že si stáhneš jenom ten malinký update.

00:45:43 Padák
A teď my jako neděláme hodinový updaty, ale tak to už je jako jenom celý den, jo, takže mně přijde jako nejjednodušší, že tady je hromada podmínek, který do sebe kodifikují nějaký kritéria a ty jsou daný tady. Tohle tady, my jsme se rozhodli říkat, co znamená, že data jsou starý a řekli jsme, že to je 12 hodin, jo. Ale pro Pavlův ceník jsou starý data, když by neměl update-lí dva měsíce třeba, jakoby, jo.

00:46:13 Padák
Pro Lebkouna může být šest hodin starý, když má nějaký go-to market, když nějaký event by na tom stavil. Každý to má jako nějak. A podle mě, jakoby...

00:46:25 Dasa
Ale počkaj, já jsem, ale tohle asi takto vůbec nemá, to se tam mají porovnávat timestampy, ane? točí je něco víc jak 6 hodin. No ale tady je napsaný jako... Já vím, že je to tam napsaný, ale v tém prípadě celá ta logika mi dává o tém, že porovnávám...

00:46:43 Padák
Jakoby já chci navrhnout, jestli to nechcem vyhodit tady tu věc. Ano. A prostě pustíš AirSync a ten ti to synkne. A je jako super rychlej, protože netáhá, on to všechno, on porovná přesnou velikost souboru na serveru a jeho timestamp modifikace s tím, co je u tebe. A pokud se to nezměnilo, tak to skipne. Takže ty ho pustíš a on... A jakoby skončí za půl sekundy prostě. A jako neskončí za půl sekundy, ale za čtyři třeba.

00:47:12 Dasa
Jo, to určitě. Len mi... Teďka. Tento script je v tém prípadě zbytečný. Já jen potřebuji vědět, proč to teda synkovalo vždycky všetko, když se to aj tak robilo AirSynkem.

00:47:24 Padák
Protože, to ti řeknu, teď jsem na serveru. Vlezu tam jenom jako kids, jdu do toho data, srdce, tam jsou parkety, a vlezu třeba na sales.

00:47:46 Dasa
Zda je tam to něco, co má nějakým způsobem... Company snapshot. 

00:47:55 Padák
Nebo můžu... Počkej, co ten, KBC telemetry, a tady jsou... Usage metric, ano. Usage metric, jo.

00:48:04 Speaker 4
A teďka... Hej, ne, prosím tě, daj mi tady jen ten zeznam.

00:48:21 Speaker 3
Já chci...

00:48:31 Speaker 4
Jenom tady vypísat, jenom ty m-timy právě.

00:48:47 Dasa
Je tamto není tak moc, prosím tě, nechaj vypísat normálně LSLA nad tou KBC Usage Metric Values tabulku, či jak se to volá. Tam to uvidíme krásně, tamto není tolko moc. Můžeš si samozřejmě zvětšit to.

00:49:08 Padák
Udělám, udělám, moment. Jo, vím to, už to vidím. Takže tadyhle mám tohle a já zkusím...

00:49:27 Dasa
Ale tady to krásně vidíš, ten timestamp je jiný.

00:49:32 Padák
Ale tak jinak, očkej. Možná to nebylo všechno, že se tahá všechno, ale strašně moc věcí se tahá.

00:49:39 Dasa
Je to, protože my máme tento, já k tomu budu hovorit inkrement, ale není to úplně asi úplně pravda, ale to je jedno, nastavený na jiba velkých tabulkách, které nějakým způsobem nám dávaly aj zmysel, co se týká na jaký slopec to prostě dát. To znamená, že naozaj teraz to jde na company snapshot.

00:50:07 Padák
Jo, jo, rozumím. Prostě vlastně všechno se ti tahá znova, kromě těch snapshotovaných.

00:50:13 Dasa
Alebo těch takových, kteří mají naozaj to, že máš rozdělené data deň po dní a oni se jakoby...

00:50:18 Padák
No to myslím, to myslím ty, jakoby...

00:50:21 Dasa
Problém je to, že používá se API, aby jsme dostali data. A API přirodzeně jako to stahování nepodporuje náš timestampový stlpec, aby jsem si stáhla iba data, které se naozaj změnily. Tam dáš ten wear filter, ale jako timestampový stlpec, jak taky jsem tam prostě neviděla. Alebo nejak, když jsem se prostě v tém hrála, tak mi to nefungovalo. Takže jsem to nemohla dát na timestamp stlpec, který mi hovorí, že rádek se změní, ale dala jsem to na stlpec datum, whatever.

00:50:57 Padák
To je divný.

00:51:00 Dasa
Hej, možná jsem ze začátku zpravila chybu až jsem to zabavila. To nevadí, to všechno je v pohodě úplně, ale...

00:51:07 Padák
Table, a to je další na mé table export.

00:51:18 Dasa
Počkej, já se tady podívám, v jakém přesně to je můj, to je něco kábec, já se podívám, jak to bude, jak se volá ten skript.

00:51:27 Padák
To musí být ale tady na tomhle tom, to máme tady, to mám, tady mám v tom repo, že jo, takže to bude.

00:51:34 Dasa
Jo, já se jíba podívám, jak vypadá, já nevím, jak se teda zvolá ten skript, který to prýmo reší.

00:51:56 Speaker 4
Takže, a to bude, já tady se nevím, jak se to bude, tak to bylo source. www.hradeckralove.org.

00:52:43 Dasa
DataSync se jmenuje a je to v srdce zložky. A jdem se podívat, jak se to tady pod mnou dělá. Máš to otevřené? Mám to otevřené. Jo, jo, ale já budu asi potřebovat scrollovat trošičku jinak.

00:53:01 Speaker 4
DataSync, co dělá, popiš mi to do detailu. Já do něj koukat nebudu. Jo, jo, tak...

00:53:22 Padák
To ten timestamp, jinak ten timestampovej sloupec s kebulí, musí to aby podporovat, protože ho používaj extraktory a vrajtry, že jo? Že když jakoby chceš zapchat vrajtrem někam pryč, tak právě on používá timestamp sloupec, když si řekne, co chce zadat a s kebuly.

00:53:37 Dasa
Je dost možné, že to nebylo jen v těch vérfiltroch, že tam jsem, lebo já jsem a o tom je pravda, že ty máš vlastně, keď si to teda uvědomím, co si nastavuješ například při transformácii, tak můžeš dát jako změna od posledného whatever, co je ten timestampový a potom tam máš ty také vérpodmínky. Jo. A je dost možné, že jsem vlastně jen netrefila, netrefila tohleto, že jsem to se snažila vezpat do té vérhovadiny.

00:54:09 Padák
Půl refresh, incremental, načte last sync for sync state, to je dobře, odečte incremental window, default 7 dní, backtracking okna pro zachycení pozdních dat, exporte jen řádky změny od change.

00:54:26 Dasa
Ne, ten incremental sync ne, čítaj tady, ten poslední c, to je, toto tam nechal ten a já tam ještě někde, tam snad je napísané, že to není aplikované, tento, partitional sync, to je to, co...

00:54:41 Padák
Aha, tak vím, co tě trápí vlastně, ty jako si exportneš celou tabulku a pak si, a v ní nemáš ten timestamp sloupec, protože on ti ho nedá, to API ti ho nedává.

00:54:51 Dasa
To API mi ho, ale já ani ne... Ne, exportujem celou tabulku, já ji už vtedy exportujem.

00:54:57 Padák
Je napsaný export celé tabulky do, co jsou v tomhle.

00:55:01 Dasa
Hej, dobře.

00:55:03 Padák
Což je OK, že jo? Nebo je to zbytečné. 

00:55:06 Dasa
Ne, ne, ne, to není OK, to nechceme robit a já si myslím, že se to tak ani nerobí, iba že se neupdatila dokumentácia. Toto máš, dokumentáci, nebo to jsi nechal vygenerovat. 

00:55:19 Padák
Z toho skriptu, tohle dělá ten skript. Já mu řekl jenom, co dělá ten Python skript a jediný, co našel, je ten Python. Pojďme pracovat s tím, že to dělá tohle, já ti řeknu, proč nemáš ten timestamp jenom, jo? Tady je, tohle, co říkáš, je skipnout, tak já ještě se k tomu na chvilku vrátím. Tahle incrementální sync, Řekne kebule, že chce data od někdy a kebula vrátí jen řádky změněné. A tam kebula použije u sebe ten timestampovej sloupec,

00:55:51 Padák
protože kebule ho má. A dostaneš jenom jako změnu. Takže když bys měla v nějakém state uložený, kdy máme poslední data, tak můžeš říct, vrať mi to za tohle období a kdyby si nechtěla vymýšlet...

00:56:07 Dasa
Věřit tě o tom, co rozumím, ale nejakým způsobem mi to tam jako nešlo použít ten timestamp na tu filtrování. Já teda bych sem povedala, že tady to nemám iba popísané, alebo že se neupdatela prostě.

00:56:21 Padák
A tohle nic popisaný nemůže, to on popisuje, co dělá ten skript. To není dokumentace, to je popis toho Python skriptu. A když máš partition sync, tak ten udělá export celý tabulky do CSV na server, třeba do toho raw folderu. Ještě taky dobrý. A můžeš se rozsekat, jak chceš, že jo? Protože jako, když bychom řekli, pojďme chvíli pracovat s tou mou původní myšlenkou, pojďme mít partitionovaný data po hodině.

00:56:51 Dasa
Jo, jo, já tému rozumím, ale...

00:56:54 Padák
Tě to například neudělá, to ty musíš udělat tímhle a máš to tady připravený. Ale jak uděláš ten sync, tak v těch output datech nemáš ten timestampovej sloupec. Proto musíš používat nějaký ver na těch datech samotných.

00:57:06 Dasa
Ale on se používá už při tom apikoli z Kebuli. Aspoň tak to určitě mělo být, protože dokonce to tam i dáváme. Hej, na toto se já zaměrám, ale určitě jsem byla v vědomí toho, že používám prostě naše storage API s tím, že už tam aplikujeme podmínku ver, to znamená jsem byla v tém, že vůbec se celá tabulka ke mně nestáhne a stáhne se jen ofiltrovaná.

00:57:33 Padák
To vůbec nevadí, jenom to debagujeme. Já jsem chtěl pojit tu myšlenku, že když by to bylo na full exportu, tak v těch datech ten timestamp není, ale ten timestamp je v tom API, takže ty můžeš tomu API říct, exportuj mi data od toho datumu, což je ta strategie inkrementální. A ta ti vrátí ty stejné data, jenom osekané a pak je do paketu a rozpartičnovat.

00:57:59 Speaker 3
No jasně.

00:58:04 Dasa
Titulky vytvořil JohnyX http://johnyxcz.blogspot.com, Do Data Description máme při každé tabulce dané podmínky, které vstupují do tohoto API callu. A jsou tam přesně ty var podmínky. Například, když půjdeš tady Company Snapshot hned pod tím, ještě, ještě, tak tady je to, ty var filtre, které vstupují do, nebo mají vstupovat, z toho, co jsem byla, co jsem tomu věřila, mají vstupovat do tohoto API callu.

00:58:38 Dasa
Takže bych se zkusila, Klóda, opýtat spíš na to, kde je tohleto aplikované, tyhle var filtre. Ale určitě na ten timestamp se teda zaměrám. Jen mi to jako na první dobrou nešlo stáhnout jako pomocí var filtra s timestampem. Rozumím, rozumím.

00:59:13 Padák
Jo, tak ty varefiltry budou v tomhle tom taky asi použitý, no.

00:59:19 Dasa
Takže jo. Já se teda zkusím do těch varefiltrů ještě raz nějak vecpat ten timestamp, ale to možno není ve varefiltroch. Jako já presně vím, jak by to bylo.

00:59:31 Padák
Mně přijde zbytečná logika, nebo podezřele komplikovaný mi to přijde od vás na tom servru, že vy prostě v Kebule máme ty data, který jako chceme mít na tom servru. A jediný, co potřebujeme je, ať nám Kebula prostě vrací změny v těch tabulkách. To je jedna ku jedný, jo. Tak proč máme data tady varefiltrů na snapshot date? Teď nás to vůbec jako nezajímá, pokud se nezměnily ty data. Jediný, co nás zajímá je, prostě na servru nemáme žádný data, vyexportují všechno na servr.

01:00:02 Padák
Vyexportují se CSVčka a s procesem si je do parketu. A teďka nastává, jako za 10 minut poté chceme update, tak prostě potřebuje mít ve state-u, nějaký metadata state-u, jako na tom serveru, kdy se to stalo, tak máme tam timestamp, kdy jsme si udělali ty exporty. A ke bůle řekneme, vrátíme zase všechny data z té tabulky, co odpovídají. Jediné podmínce, že jsou větší, než je tahle ten timestamp.

01:00:30 Dasa
No jasně, ne, já rozumím temu, ale jak jsem povedala, v podstatě to jišlo jiba o to, že jsem chtěla dorešit velké tabulky, ale timestamp mi na první dobu nefungoval.

01:00:44 Padák
To bych se tam měřil, ten jako vlastně to obyčejný inkrementální export tabulky, kde je jedinej parametr, jaký data má vytýct. A ještě mu řeknu, jaký kebula, aby se na export dat používá. Možná to tady napsal někde.

01:01:17 Speaker 3
Myslíš Endpoint, jo. 

01:01:19 Speaker 4
Aha.

01:01:25 Speaker 3
Taky čtá jako úplně zblbné, když to nemusí písat sám.

01:01:39 Padák
Ale jinak za mě můj průřez s tím updatem, jako jsem udělal, máš dobrý postřeh, že teda ty M-timy tam vlastně do toho nevstupují, jenom to Export a Sync, no, tak tohle to bude. správný Endpoint. Může to SDKčko něco neumět, jo. 

01:02:14 Dasa
Já to nerozumím. Tak nepodporuje, ale pritom jich používám.

01:02:18 Padák
Já ti mluvíš o čem. 

01:02:22 Dasa
Počkej, SDK Kebula Storage nepodporuje Wear Filters. Aha, přímo, jo, přesně, jo, jo, jsem jeba nedopuštila.

01:02:31 Speaker 4
No a tohleto, Export Async je Storage Tables.

01:02:51 Padák
Počkej, já koukám do dokumentace teďka, jo. Export Async a je to tohle, ještě tady, je to tenhle ten endpoint. My nepotřebujeme filtrovat usery ani where-filtry. Tohle podle mě, jo, tohle hodně posunuli kluci.

01:03:26 Padák
Urosorting, caching, chip result.

01:03:32 Speaker 4
Change synths, filtering by, aha.

01:03:36 Padák
Deprecated, change synths, jasně, daj si deprecated, change synths.

01:03:50 Speaker 4
Ok, takže já jsem teda...

01:03:53 Padák
A tohle ti šahne na ten timestamp.

01:03:56 Dasa
Ano, ano, ano, takže já jsem to jíba se snažila spat do...

01:04:01 Padák
No, a tím si to strašně jako zjednodušíš, no.

01:04:06 Dasa
Určitě, určitě.

01:04:09 Padák
Ale já zkusím navrhnout ještě nějaký, když to bude úspěšný, tak přišlo by mi super, kdyby jsme ty updaty z Kebuli dělali co 15 minut. A my tam co 15 minut nemáme nový data, takže se nic nestáhne, když použiješ ten ChangeSense. Ale na naší straně by byly 15-minutové refreše.

01:04:37 Dasa
Asi úplně, jak jsi povedal, nic se nestáhne, bude to vlastně jíba prázdný job.

01:04:43 Padák
No jasně. A problém máme v tom, že z naší telemetrie máme data pomalu. Z různých důvodů. Ale tady v tomhle ten moment, kdy se to naše řešení připojí na data, tak si je refrešuje co 15 minut. A ty, když budeš mít ten Change Sense, tak to prostě jenom sebehne rychleji, fresh prostě a zjistí nic, jako nemám.

01:05:16 Padák
A asi jako dává, když budeme mít, kdyby jsme měli každou tabulku ve vlastním foldru, což teď máš na ty poměsíčních snapshotech, tak nám to dá 360, když to bude po hodinách, kdyby to byly ty čanky, tak by jsme měli 8700 souborů za rok na každej ten adresář,

01:05:52 Padák
což by nakonec asi pro ten AirSync neměl být absolutně žádný problém. Teď bych šel touhle cestou a když se to jako uchytí, tak vždycky můžeme starý data spojovat, že jsou po měsících a jenom aktuální měsíc po hodinách třeba. Nebo něco takový, ale to si teď nemusí vůbec komplikovat tím.

01:06:18 Dasa
Jo, já to budu muset ještě premyslet nějakým způsobem, jak se například potom zachovat, keď se tam potřebujeme něco premáznout v těch tabulkách a podobně.

01:06:38 Padák
No tak se refrešne ten danej kus dat, že jo? Jo, takhle chápu, smažeš data a my je smažem a nemají, jakože, no tak budeme muset udělat jako full delete, full export v tu chvíli, no.

01:06:52 Dasa
A na klientské straně se to...

01:06:55 Padák
Ale to je přece, Dašo, úplně stejný jako s jakýmkoliv systémem, ne? Když smažím jako měsíc dat v Kebule, tak musím udělat full load té tabulky.

01:07:05 Dasa
Samozřejmě. Já jen rozmýšlám, jak to presně znamená potom na tej, že to budeme muset dořešit i na tej klientské straně, že jim to tam budeme muset jako full loadem nejakým...

01:07:15 Padák
Nemusíš vůbec nic, o to se postará ten AirSync. AirSync se nezmaže. 

01:07:20 Dasa
Zmaže. No jasně, ten ti.

01:07:22 Padák
synkne ten folder, aby odpovídal tomu z toho tý protistrany. Takže když si do toho svýho foldru něco dáš a není to na serveru, tak se ti to delete u tebe.

01:07:33 Speaker 3
No, dobré, dobré.

01:07:35 Padák
No a tím, že máš jako, když budeš mít change sync tady, takhle jde čas a tady se to změní, tak nám prostě v té nový 15-minutový dávce přijíjou úplně všechny data, tím se to celý regeneruje a ke klientovi přiteče úplně všechno.

01:07:50 Dasa
Jo, jo. Dobré, já tady ten timestamp, teda já to obnovím a teda evidentně jsem išla jíba do blbého, blbého, blbý parametr jsem dávala na ten endpoint.

01:08:05 Padák
Chceš mocknout ten AirSync, já ti jako já, jako ruku bych si za to useknout nenechal, ale potřebuješ to radši vyzkoušet, jo. Tak tady jsem na mém servru, McAdeer test AirSync. No tady, ještě konečně se nepotřebuji na tohle, nevím, co mi tady vyrábí, vždyť mám si tady nové.

01:08:37 Dasa
A prosím tě, vím, že jsi to hovoril vlastně na začátku, ale netuším si, či... Teda úplně mi uniklo to, či si to dorešil, alebo to jiba považuješ za issue. Teda z AirSync je teda na data, alebo si to už prehodil na to, aby to bylo na veškerou dokumentáciu a podobné. Je to všechno. 

01:08:55 Padák
všechno se v server folderu se synkuje a všechno, co jsem ti teďka řekl, jsem zároveň update-nul do server MD. Když si pustíš kóda, řekneš mu, popiš mi, co se změnilo v server MD od včerejšího rána. Tak on si z G2 vytáhne změnu a poví ti to. Takže a co já chci tady je AirSync, tenhle tenček jsem teďka jako nedám a bude to data, a tohle se jmenuje.

01:09:28 Padák
test sync. a ten jsem tady nevyrobil si. Prosím. Test Sync.

01:09:40 Speaker 3
No něco jsi za něho dal, ne. 

01:09:42 Padák
Zatím jsem tam nedal nic. Takže tady mám prázdnej adresář, když já to takhle zaklapnu. A teď jsem tady, touch test 1, ale to jsem udělal dva soubory teďka. To nevadí. Takže teďka, když tady pustím AirSync. 

01:10:07 Speaker 4
homepad, aha. 

01:10:14 Padák
proč tam je homepad, better test Sync, to nemá absolutní cesty.

01:10:23 Speaker 4
GitHub, Kebula, Data Analyst, Test Sync.

01:10:34 Padák
Jo, počkej, už to vidím. Já to není test sync, ale test rsync. Tady jsem to měl dobře. Test rsync je na tom servlu, se jmenuje. Takže mě to teď přeneslo dva soubory. A tady, když dám echo do test, a udělám touch 44,

01:11:06 Padák
tak tady mám choose a tady teďka tohle přijmenu na 454 a vyrobím tady novej file ahoj a do filu ahoj, něco napíšu. Takže teďka tady test, sync. Ahoj, tak je tamto FEDESO, FEDESO. Teď pustím AirSync a zůstal tam.

01:11:37 Padák
Ale to bude teda OK, ale počkejte.

01:11:40 Dasa
Hej, Peťo, já si nemyslím, že to potřebujeme vyrašit teraz. Já to tady mám zapsané, aby v momentě, kdy budeme rešit tohleto, aby jsme se na to zamírali. To bude už v rámci tej celej flow, prostě incrementu rozparsované po jedném dní a podobné věci, takže to se potom... A možná se to ani nemusíme rešit.

01:12:11 Padák
A to bude nějaký přepínač. Minus, minus, delete. A že to funguje takhle.

01:12:17 Dasa
A nebo nějaký force, nebo nějaká taková hovadina tam bude.

01:12:21 Padák
A teď to vypadá jako ten na tom servru. Takže když dám touch 33 a tady vyrobím new file a ten se bude jmenovat 33 taky a bude v něm tohle a tady do toho dám ahoj do 33. Tak teďka tady koukám na ten kript a teď pustím airsync a ahoj.

01:13:00 Padák
Takže přidáme tam parametr minus minus delít.

01:13:03 Speaker 3
Jasně, jasně.

01:13:05 Dasa
Dobre, hej, takže pro mě to vypadá, že se dneska budeme hrát ještě s datama. S tím, co tam máme my, co tam máme nějakou věc a s tím synkem celkovo.

01:13:21 Padák
Hele, chceš to tak, že když budeš ty věci dělat v branchi a uděláš pull request, tak já ho klidně jako zreviewuji.

01:13:31 Dasa
Hej, to, že jsem včera vymazala súbor, byla náhoda.

01:13:37 Padák
Kvůli tomu to neříkám. Jakože kdybychom tohle udělali předtím, tak já bych ti asi odchyt tu logiku, že tam stačí použít change since a nemusíme dělat složitý čeky dát.

01:13:51 Dasa
Fakt to nebylo o tom, že bychom to v hlavě neměli. Spíš jde o to, že jsem to teda jako jíba vzdala príliš skoro. Ok, nechceš. Já, když budu mít nějaké připomínky, tak ti to klidně napíšem v nějakém jako sumári a uvidíme. Já teda seště potřebujeme dorešit nějaké telemetrické data, taky zkontrolovat, aby jsme potom věděli, že jsou správné a mohli jich sem nasinkovat. Bude to taková flow, ani nevím, když se k tomu úplně dostaneme, ale určitě se tomu dneska budu věnovat.

01:14:29 Padák
Hele, a teda já jsem... Máš 5 minut. 

01:14:33 Dasa
Jo, určitě, určitě.

01:14:36 Padák
Řekněte tam tu telku, prosím. Tady do toho, tady jsem měl rozjetý ten Cloud Settings, jo? Takže máme přidaný deny, tady je deny na čtení, tady je write and view credentials, secrety, a tady je write a edit na server. Jo.

01:15:01 Speaker 3
To je skvělé, ne. 

01:15:04 Padák
Takže teďka to vyzkouším, jo. 

01:15:08 Speaker 3
No určitě.

01:15:10 Padák
Pustím si novýho Cloda.

01:15:15 Dasa
Jaký je prosím tě rozdíl v těchto slovách write and edit. 

01:15:20 Padák
On do toho nemůže zapisovat a edit, že to nemůže editovat. Fakticky do toho zapíše v obou případech, ale on má třeba, já nevím, nepustí AVK nad tím podle mě, víš, nebo tak. Ale jako oboje tam dělá modifikace, ale podle mě appem, víš, jakože tam nedá echo a nepřipíše. Tak já vám teďka řeknu, podívej se na notifications,

01:15:57 Padák
server docs notifications a řekni mi, kam se posílají, Telegram nebo Slack. On mi řekne, že Telegram a já mu řeknu, ať ho napíše kapitálkama do toho dokumentu.

01:16:13 Speaker 4
A co ti mám dát. 

01:16:15 Padák
Prosím. Úprav dokument. Úprav ten dokument. Dokument. Patě. Telegram napsaný kapitálkama. Tedy Telegram.

01:16:36 Speaker 3
Jsem zvědavá. Já taky.

01:16:40 Padák
Ale to klapne. Takhle to funguje, no.

01:16:44 Speaker 3
Skvělé.

01:16:46 Padák
Ne, počkej, to je z Cloud MD ještě. Ano, chci. To je ta first line kontrola.

01:16:55 Speaker 4
Musíš odhodit všechny karty, aby ty zůstala rovnou. A já? Ne.

01:17:02 Speaker 3
To je jako fajn. Super.

01:17:09 Speaker 4
Takže... Ne, ne. Ještě mě napadá teďka, já udělám rychle naslující věc, do settings nastav, že všechny server scripty můžou pouštět bez ptaní.

01:17:47 Padák
Vyrobíme ten jakoby settings.json, který dáme do toho init skriptu novýho prostředí, user si do něj něco bude doplňovat, ale v základu to bude dělat jako věci tak, jak my chceme, a samozřejmě on ho může přepsat a odebrat mu ty zákazy, ale bude to jako nastavený, prostě správný. Tak, že tady, a teďka, tohle je moje totiž, jo, to jsou mý nějaký věci, tak ty já, jakoby, tohle smažu.

01:18:19 Padák
Dobře, tak jakoby, nechceš rebase bez dovolení, někdo může srát komitnutí, já jedu jako na YOLO, víš, ale Fetch div, status, remote tag, find, getls, tree, head, tail, dot, switch, var, pvd, hu, mi, echo, file, stat, to je dobrý, bash, server, script, python, server, script, web, fetch, tohle, to jsou allow, dobře, ať může lísnit na GitHub, to je divný.

01:18:54 Padák
Search, tak, a Deny, Envy, Credentials, Secrety, Pemiky, Keystores, tohle si myslím, tam jsou ty privátní klíče, to jsou to báky, jich jako default, to bych jako nezasehovala, Password, Token, Apiky, vrajty do toho, Credentials, Secret, Pem, Envy, dobrý, a tady je naše server, a Ask, a to ať si dělá ten user.

01:19:25 Speaker 3
No, jasně.

01:19:27 Padák
Ať se ptá na RMM, ať se ptá Reset, Clean, Push, Force, Push. A Deny jsme měli RMM? Ne, ty můžeš chtít ať něco vyčistí, ale ať se tě zeptá na to, že jo.

01:19:41 Speaker 3
Dobré. Vybušle, katávka, on už vám sem dokonečil.

01:19:45 Padák
Ať tím, že Composer. Jo, tak tohle mi přijde jako dobrý teďka.

01:19:50 Dasa
Jo, rozumím.

01:19:52 Padák
Já řeknu, vezmi celý settings a dej ho do nového GitHub issues, které přiřadí Daša Dama.

01:20:11 Speaker 3
Jo.

01:20:12 Padák
Jak to bylo? Myslím, že jo. Daša Dama, aby tohle nastavení dala do původního initu prostředí. Hele, to je za mě asi všechno. Co já udělám, je, že dotáhnu ty notifikace, přidám tam časem Slackovou podporu.

01:20:45 Padák
Upravím ty věci, co jsem si tam nastavil, že mi tam dělají blbě to pouštění z toho ručního exekutí toho reportu a potom tady mám experiment můj, budu dělat appku do MacOSu, která běží ve status baru nahoře v desktopu a umí ti to zobrazit, tu notifikaci. Takže tam, to je můj experiment, takže tam dělám to tady v MacOS app branchi a zbuilduju prostě install scriptík, který ti umí to do kompu nainstalovat, takže takovým tom takhle, já nevím, tady mám třeba, já třeba mám password, víš, tak tohle, že ti tam, to nevidíš, on to nenazdílal.

01:21:40 Padák
Takhle, tohle popup. Takže to si zkouším udělat, že mi přijde fajn, že by vlastně si mohli z CSU deploynout nějaké notifikace, které by se jim objevily v kompu. A vlastně to pustíš z hlavy a prostě ti tam blikne, hele něco se děje a můžeš se vrátit k té debagnouci detailu. A kvůli tomu vlastně mě pak přijde super atraktivní mít ty 15-minutové updaty třeba.

01:22:11 Padák
Asi jakoby rychlejší to není potřeba v tuhle chvíli. A ještě chci říct jednu věc. Já jsem... A pak končíme, už si fakt nebudu dál zdržovat. Tady máš ještě minutu.

01:22:24 Dasa
Já doufám, že tam nemám žádný mít. Ne, nemám.

01:22:28 Padák
V tom našem internal repu. Jsou v Issues popsané tři věci, co jsem odtegoval jako Cloud Learnings. A tohle byla věc, kterou dostal Maňano za úkol, že jí chce Anneli z Vikingů, aby jsme jim dodali data. A já jsem to vzal, copy-pastnul jsem to a nechal jsem to clouda zpracovat a vyrobil z toho ať to já hotml a ukazoval jsem vám ho. 

01:22:53 Dasa
Myslím, že ano.

01:22:55 Padák
A Matějkys tam pak začal dodávat nějaký data a sedli jsme si včera spolu na chvilku kvůli tomu, nebo předevčírem. Určitě jsme u toho ale jako nebyla mě, jsme spolu jako konverzaci a on říká, tím ho zdravím, zároveň se bude poslouchat. On říká, že třeba máme Infrastructure Cost Data a že máme SRE, co to poskytuje a říká, že Lucka dělala nějakou query na to, co to procesuje a že než to tam přidá, tak by ty query chtěl udělat jako review.

01:23:22 Speaker 3
Já to budu dělat dneska.

01:23:25 Padák
Já jsem mu říkal, a tak to je super, že si to říkáš, protože to chci i tobe zasadit do hlavy. Udělejte to tak, jak to máte v plánu, jo. Ale moje myšlenka je následující. Podle mě je správný do těch dat, do těch parketů, dostat ty data od SRE, ne nějakej post-processing z kebuly. A tu query, kterou jdeš reviewovat, tak tu chceš dát jako example Claudovi. Nebo jí zmaterializovat jako Doug D.B. Viewčko nad tím. Protože efektivně, kdyby jsme přišli uměle,

01:23:58 Padák
ta věc neexistuje, ten požedevek, ale kdyby se měly dělat minutový updaty, jo. Tak dělat je v Kebule můžeš, nebo dvouminutový, to je jedno, protože můžeš jít dělat v Kebule, ale bude to stát 5000 dolarů na Snowflake kreditech měsíčně. Ale fetchnout ty data, jenom protít ty data do těch parquet filů je zadarmo. Nakonec by jsme mohli i bypassnout tu Kebulu, protože problém v Kebule v tu chvíli je to, že si je Storage API ukládá do Snowflake, kde je nepotřeba mít. Čili ty vemeš file od SRE, dejme tomu kosty Azure,

01:24:30 Padák
pošleš je do Kebuly, ta je dá do Blob Storage, nebo do S3, store je do tabulky ve Snowflake, a ty je pak chceš z toho vyexportovat, tak se vyexportují do S3 a stáhnou se na server. A my potřebujeme vyřadit tato spodní část, tu Snowflake tabulku, protože my v ní neděláme žádné nýpočty. Takže když přijdou data do toho parquet filů, stáhnou se AirSynkem na lokál, tak teď my máme prostě 100 MacBooků, jako vím, že pár Mešuganem mám MacBooky, jo, ty jsi jeden z nich a Odin a prostě tak.

01:25:04 Padák
A v nich máš prostě 20 gigaramky, 8 CPUček v jader a prostě si to můžeš jako takhle, je to zadarmo ten post-processing rychlej. A jakože může specificky na tyhle rychlejší data dávat super smysl, nesnažit se vytvářet jako kebule ve snowflaku tabulku, která je jakoby správně, ale my potřebujeme tu query, kterou jdeš reviewnout, ale tu chceme dostat do toho RGB, protože pak můžeš refrešovat data a lidi je mají fresh,

01:25:39 Padák
kdykoliv se na to podívají. Akorát, že ne každej se je potřeba refrešovat z toho servru pořád. Každej se to udělá tak, jak potřebuje vlastně. Když pak přijde a řekne Martin Lepka, nechť platí, že máme live data v těch parquet filech, somehow, a on řekne, já chci sekundový updaty, no tak si šahaj do svý DuckDB každou sekundu a jemu to bude žhavit SSD-čkovej disk prostě u něj na lokále. A Anička Dušková potřebuje jednou za 20 minut si to updatenout. Víš, tak se do těch DuckDB dívej jednou za 20 minut a předtím si pust ten update,

01:26:12 Padák
který AirSync tě stáhne třeba, nevím, plácnu 30 kilobajtů dát, víš, za těch 20 minut. Nebo 300 kilobajtů, je to jakoby sub-second věc. A je to vlastně jako hrozně zajímavý pohled na tu architekturu, že jakoby ten výpočet offload než na toho klienta. Moje zkušenost je, že když mám ten parket a už mám ten Python script, který dělá tu query přes tu DuckDB, tak je to výrazně rychlejší, než to dělat ve Snowflake. A k tomu pak padá příběh Tomáše Trnky z Carvaga, který mi říká, v prosince mi říkal, my jsme začali, přestali používat SQL ve Snowflake.

01:26:48 Padák
Data máme ve Snowflake, ale pouštíme tam ten jejich Snowpark, čili Python job, kterým si nahodíme lokální data DuckDB a uděláme tu SQL query v té DuckDB. A je to levnější a rychlejší, než dělat Snowflakeový SQL. A používají celý Snowflake infrastrukturu. No a tak já chci vlastně říct, že... Možná ta query, kterou budeš reviewovat, efektivně vede k tomu, že nemůžeš mít díky tomu 15-minutový update těch dat,

01:27:19 Padák
byť třeba můžeš mít 15-minutový update, možná můžeme mít live stream kostů z API a VS. Třeba ho nepoužíváme, my to ani nemáme use case, já jenom kalibruji ten pohled na tu architekturu a Kebula je super a správně, jenom je tam na hovno ten snowflake, který je drahej a pomalej vlastně, oproti tomu, co my děláme teďka s tou DuckDB. A my budeme potřebovat dostat do Kebuly DuckDB backend. A v tu chvíli se ta hra úplně jako změní. A kdyby nám pak Kebula uměla sypat ty parquetfile, tak by jsme na tom serveru měli jenom ty parquetfile.

01:27:59 Dasa
Možná to je jako, já vlastně nevím, jakým způsobem to funguje, ale už teda si já mám párkrát nastavené, že do například do datové apky mi jdou parkety přímo.

01:28:11 Padák
Protože si nepoužíváš storage apy, toto neumí. Snowflake samozřejmě parket file podporuje, že Snowflake documentation a tady bude, je to copy nebo export.

01:28:28 Dasa
Já si myslím, že na input mappingu to máme. Parkety.

01:28:32 Padák
Jako fakt. 

01:28:35 Dasa
Já naloadujem tabulku a dám to, nech to je v parketoch.

01:28:45 Padák
Jako v kebuli v ujičku to klikáš. 

01:28:47 Dasa
Ano, ale není, alebo takto, není to v ujičku, ale když jdeme přes debug mode, tak tam dám tuším. klíčové slovo file a parket a mám parkety.

01:29:05 Padák
Zajímavý. To je ale ten async export. Ty chceš unload data a říkáš...

01:29:17 Dasa
Pojďte, tak já ti to ukážu. Možná se jen nerozumíme. Počkej, já si to tady někde otevřu.

01:29:22 Padák
Tohle jsou data, co umí Kebula. JSON anebo CSV, to je to RFC.

01:29:29 Dasa
Hej, možná hovorím něco, co si nerozumíme, ale...

01:29:37 Padák
Jo, to je ono, už to vidím. Ale neumíš tomu říct, že chceš ten třeba hodinový export, že jo. 

01:29:48 Dasa
Ne, ne, ne, to jak je to potom rozčleněné, to vlastně nemám vůbec. vůbec v rukách.

01:29:57 Padák
No a nemůžeš tohle použít teda teďka taky v tom API, jako vlastně říkat si, ať ti vrací parkety, oni budou mnohem menší.

01:30:06 Dasa
Mě se předstává, co potom s týma datovýma typama tam.

01:30:10 Padák
Jo, vidíš, no stejně by se musela předělat. Já jsem myslel, nech to, jak to máš, máš v pravdu, to je v pořádku. Dobrá, tak to je jedno. Tak tohle jsem jako chtěl prostě jenom, ti zase i do hlavy přemýšlet nad tím, takže některý věci jako nemusíme nutně v Kebule vypočítat, protože je můžeme přemýst jako v těch, jako na... A často to může být, že to třeba je jako, že ten výpočet bude někdy dělat jako nějakou očištění a sebere věci, to říká.

01:30:41 Padák
Matěj Kizvono, to bude asi platit na T-Query, že tam... Je mnoho způsobů, jak ty data interpretovat a ta query zastabilizuje, takhle se na to chceme dívat a to je legitimní, ale pak může být query, která ty data třeba omezí nějakým způsobem a furt jsou dobrý jako example a může být fajn je dávat do těch příkladů Clodovi. A Matěj říká, to píše ve Slacku, nebo mi to vlastně psal možná v WhatsAppu včera,

01:31:11 Padák
že tu jdu na poně na příští týden řešit ty certifikovaný query, ty z toho udělají tu top odpovídačku.

01:31:22 Dasa
Já ti to za chvíli nabíjem, Nory. O toho. Hej, počuji, možná ještě jeden dotaz ovládně dobré. Parketu a kolkokrát tady prenesení věcí na klienta, nemohlo by se to kastování těch datových typů robit taky až na straně klienta. 

01:31:42 Padák
Ale mohlo. Mohl, pokud. Jakoby bude to muset každej dělat furt dokola a nesmí na to zapomenout. Když to uděláš jenom do týda k DB, tak hrozí, že si ale může sáhnout, jako klod si šahne občas na ty parkety přímo, protože mu třeba nesedí struktura těch view chain, víš. Tak jako přijde mi správný, aby ty parket faily byly jako top notch, prostě eňuňuňu jako udělaný.

01:32:13 Dasa
Jo, jo, jo, dobře.

01:32:14 Padák
Spíš bych to nedělal tady to, no.

01:32:15 Dasa
Dobře, dobře, OK, tak v tom případě takhle. Takhle budeme pokračovat. OK, OK.

01:32:23 Padák
Dík moc.

01:32:24 Speaker 3
Já děkuju.

01:32:27 Padák
Čili já doladím svý notifikace, ty na nic jako nešahaj, ty se podělí na to jednotlivý GitHub, jako na nic z toho okolí, ty se podělí na to GitHub, píšu, jestli se běhají tablety dobře, když tak odstraň ten ček sam z toho Sync scriptu a nic z toho nemá vysokou prioritů zároveň, chci říct, že to jako počká, to nemusí se dít jako teď a já přijdu s tou desktopovou appkou.

01:32:54 Dasa
Jo.

01:32:55 Padák
V pláně se bude dobrá, jako.

01:32:56 Dasa
Tak já okrem toho, že tady nějaký Sync budeme řešit, tak to je ta taková jako flow spolu s týma inkrementálním vztahováním. Jo. Takže to bude, no. A ještě se podíváme na tu kverinu tých kostů.

01:33:13 Padák
A já ti pak zupdatoval nějaký update script a nevím, jestli je správný, samozřejmě, který dělá ty... ten update, ten banner available data, tak to jenom jako ať, když tak ho cooptuji.

01:33:25 Dasa
Jo jo, kdyby náhodou mi tam něco, tak se doptám jaká byla změna.

01:33:31 Padák
Kdyby si po mně něco chtěla jako za odpovědět nebo udělat review toho pull requestu, já do Slacku koukám jenom když u toho sedím a napadne mě tam koukat, ale když ho napíšiš na Whatsappu, tak to budu na to reagovat rychlejc.

01:33:46 Speaker 3
Dobre, dobré. Budu na to pamatovat.

01:33:49 Padák
A číslo máme Slacku.

01:33:50 Speaker 3
Díky, čau čau, ahoj.
