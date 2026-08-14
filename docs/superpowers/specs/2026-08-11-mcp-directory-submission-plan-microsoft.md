# Plán: oficiální MCP connector Agnes v Microsoft ekosystému (Copilot Studio + GitHub Copilot)

## Rozsah

Protějšek k
[`2026-08-11-mcp-directory-submission-plan.md`](2026-08-11-mcp-directory-submission-plan.md)
(Claude/Anthropic) a
[`2026-08-11-mcp-directory-submission-plan-openai.md`](2026-08-11-mcp-directory-submission-plan-openai.md)
(OpenAI), tentokrát pro **Microsoft Copilot Studio** a **GitHub Copilot**.
Jde o dva odlišné produkty (Copilot Studio = Power Platform / M365 agent
builder, GitHub Copilot = dev-tooling), s odlišnými submission modely, ale
oba spadají pod Microsoft, takže je držím v jednom souboru.

**Poznámka k zadání:** odkaz na
`learn.microsoft.com/en-us/microsoft-copilot-studio/knowledge-copilot-connectors`
řeší jinou funkci — Microsoft Graph "Copilot connectors" pro indexaci
firemních dat jako knowledge source do M365 Copilot vyhledávání. To
konfiguruje tenant admin ručně přes Microsoft 365 admin center, není to
submission proces pro třetí strany a nesouvisí s MCP. Pro Agnes jako
MCP-based tool jsou relevantní jiné dokumenty (viz níže).

---

## Část A — Microsoft Copilot Studio

### Klíčové zjištění: neexistuje jeden centrální "app directory" jako u Claude/OpenAI

Microsoft má pro MCP servery v Copilot Studio **tři different cesty**, žádná
z nich není přímý ekvivalent Claude/OpenAI vetted directory:

1. **Per-maker manuální připojení (MCP onboarding wizard)** — kterýkoli
   maker agenta v libovolném tenantu si ručně přidá MCP server zadáním
   Server name/description/URL + auth typu. Žádné schválení Microsoftem,
   žádná veřejná viditelnost pro ostatní zákazníky — je to ekvivalent
   "custom connector" fallbacku, který máme i ve Fázi 5 Claude plánu.
2. **Custom connector přes Power Apps (OpenAPI)** — manuální alternativa
   pro makery, kteří chtějí víc kontroly (vlastní OpenAPI schema soubor
   popisující MCP server). Opět jen pro daného makera/tenant, ne veřejné
   listing.
3. **"Bring your own (BYO) MCP server" přes Agents 365 CLI + M365 Admin
   Center** — server se registruje a "once properly registered and
   approved, becomes available for use in Copilot Studio". Zatím nejasné,
   jestli je schválení per-tenant (pro vlastní organizaci) nebo cross-tenant
   (viditelné pro cizí zákazníky) — **nutno ověřit**, doc:
   `learn.microsoft.com/en-us/microsoft-365/admin/manage/manage-tools-for-agent#bring-your-own-byo-mcp-server`.

**Skutečný ekvivalent "oficiální/certifikovaný, viditelný napříč všemi
zákazníky" je jiný program:** Power Platform **Connector Certification**
(`learn.microsoft.com/en-us/connectors/custom-connectors/submit-for-certification`).
Tohle je starší program (předchází MCP), ale certifikované konektory se
zobrazují napříč Power Automate/Power Apps/Logic Apps/Copilot Studio pro
všechny zákazníky — je to blíž modelu Claude/OpenAI directory než cokoliv
MCP-specifického.

- Dvě cesty: **Independent Publisher** (PR do `microsoft/PowerPlatformConnectors`
  na GitHubu, komunitní review) nebo **Verified Publisher** (submission přes
  Partner Center, formálnější).
- Certifikace zahrnuje swagger validace, manuální endpoint validace,
  bezpečnostní validace.
- **Důležitá podmínka:** certifikované konektory musí být **open-sourced**
  pro komunitní příspěvky — nutno ověřit, jestli to znamená jen definici
  konektoru (OpenAPI schema/manifest), nebo očekávání širší otevřenosti.
  Agnes je "source-available", ne plně open-source pod OSI licencí — může
  to hrát roli, potřeba prověřit přesné znění požadavku.

### Technické požadavky (z MCP onboarding wizard dokumentace)

- **Transport:** pouze **Streamable HTTP** — SSE je deprecated a Copilot
  Studio ho po srpnu 2025 nepodporuje vůbec. Agnes už na Streamable HTTP je
  ✅ ([app/api/mcp_streamable.py](../../../app/api/mcp_streamable.py)).
- **Auth:** None / API key / OAuth 2.0, přičemž OAuth 2.0 má tři podvarianty:
  - **Dynamic discovery** (server má DCR + discovery endpoint) — tohle
    odpovídá tomu, co už Agnes implementuje (RFC 8414 `.well-known`
    discovery) ✅, nejjednodušší cesta.
  - Dynamic (DCR bez discovery)
  - Manual (ruční Client ID/Secret/Authorization URL/Token URL/Refresh URL)

### Fáze 0 — Ověření modelu (týden 1)

- [ ] Ověřit, jestli BYO MCP server přes Agents 365 je tenant-scoped nebo
      cross-tenant schválení
- [ ] Prostudovat Power Platform Connector Certification požadavky detailně
      (`learn.microsoft.com/en-us/connectors/custom-connectors/certification-submission`)
      a ověřit open-source podmínku vůči Agnes "source-available" modelu
- [ ] Rozhodnout: jít cestou Independent Publisher (rychlejší, komunitní PR)
      vs. Verified Publisher (Partner Center, formálnější, případně
      vyžaduje existující Microsoft Partner status)
- [ ] Pokud open-source podmínka nesedí k source-available modelu Agnes,
      zvážit fallback: jen dokumentovat "BYO MCP server" / custom connector
      návod pro zákazníky (bez veřejné certifikace)

---

## Část B — GitHub Copilot

### Klíčové zjištění: otevřený self-publish registr, ne vetted review

GitHub má **GitHub MCP Registry** (`github.com/mcp`, public preview) —
zásadně jiný model než Claude/OpenAI:

- Publikace přes CLI nástroj `mcp-publisher` (stejný jako obecný
  `modelcontextprotocol.io` registry)
- Ověření vlastnictví namespace `io.github.<org>` probíhá přes **GitHub
  OAuth**, ne přes lidský review team
- Žádný byznys/identity verification proces jako u OpenAI, žádný formulář s
  10+ kroky jako u Claude — je to blíž npm/PyPI registraci než "app store"
  submission

### Discovery pro uživatele

- VS Code: vyhledání přes `@mcp` v Extensions panelu
- JetBrains/Xcode: MCP Registry okno v IDE
- Visual Studio/Eclipse: registry i manuální konfigurace
- Manuální fallback vždy dostupný přes `.vscode/mcp.json` / IDE-specific config

### Organizační policy vrstva (nezávislá na registraci)

- Copilot Business/Enterprise admin má policy toggle "MCP servers in
  Copilot" — **defaultně vypnuto**. I kdyby byl Agnes v registry, zákazníkovi
  to nic nedá, pokud admin MCP nepovolí.
- Není enterprise-level allowlist na úrovni "které registry/marketplaces
  smí Copilot CLI uživatel přidat" (otevřená komunitní diskuze, zatím bez
  řešení) — bezpečnostní háček, o kterém stojí za to zákazníky informovat v
  dokumentaci.

### Fáze 0 — Kroky (týden 1)

- [ ] Zaregistrovat `io.github.<org>` namespace přes `mcp-publisher` (nízká
      bariéra, žádné schvalování obchodní identity)
- [ ] Ověřit, že server splňuje registry metadata požadavky (server.json
      manifest dle `modelcontextprotocol.io` specifikace)
- [ ] Zdokumentovat pro zákazníky, že i po registraci musí jejich
      Copilot Business/Enterprise admin explicitně zapnout MCP policy

---

## Souhrnné srovnání napříč platformami

| | Claude | OpenAI | Copilot Studio (MCP) | Copilot Studio (Connector cert.) | GitHub Copilot |
|---|---|---|---|---|---|
| Model | Self-serve directory review | Self-serve review + identity verification | Per-tenant/maker, žádný cross-tenant directory | Cross-tenant, ale samostatný starší program | Self-publish open registry |
| Self-hosted podpora | Ano (custom-URL připojení) | Jen pro "trusted" partnery (template URL) | Ano (každý maker si přidá svou URL) | Nejasné — vyžaduje open-source konektoru | Ano (URL je součást manifestu) |
| Vetting | Ano, formulář + review tým | Ano, identity verification + review | Ne (per-tenant) / Ano (certifikace) | Ano, formální certifikace | Ne — jen namespace ownership |
| Blocker k vyřešení | Nuance per-org vs per-user URL | Established relationship s OpenAI | BYO scope (tenant vs cross-tenant) | Open-source podmínka vs. source-available Agnes | Žádný zásadní — nejnižší bariéra |

---

## Otevřené otázky k dořešení

1. Je "Bring your own MCP server" v Agents 365 tenant-scoped, nebo umožňuje
   cross-tenant viditelnost bez plné Connector Certification?
2. ~~Znamená open-source podmínka Power Platform certifikace jen otevřenost
   OpenAPI/manifest definice konektoru, nebo širší nároky na produkt?~~
   **Vyřešeno (research 14.8.):** viz níže — jen definice konektoru, ne API/produkt,
   a navíc existuje novější cesta bez téhle podmínky vůbec.
3. Má organizace existující Microsoft Partner Center účet pro Verified
   Publisher cestu, nebo bychom šli přes Independent Publisher (GitHub PR)?

### Vyřešeno: open-source podmínka neblokuje Agnes (research 14.8.)

Ověřeno přímo v `learn.microsoft.com/en-us/connectors/custom-connectors/submit-for-certification`:

> "You're only open-sourcing your connector files, not your API. This data is
> already accessible to users through the Microsoft Copilot Studio and
> Microsoft Power Platform public APIs."

Tzn. i u **starší** connector-certification cesty se do
`github.com/Microsoft/PowerPlatformConnectors` otevírá jen definice konektoru
(swagger/manifest), ne underlying služba — source-available licence Agnes by
tomu nestála v cestě.

**Důležitější zjištění:** pro MCP server konkrétně existuje **novější, přímější
cesta** — "Microsoft MCP server certification (preview)", offer type
**"Apps and Agents for M365 and Copilot"** v Partner Center
(`learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-certification`),
nezávislá na Power Platform connector-certification popsané výše. Requirements
podle té stránky **open-source vůbec nezmiňují** — místo toho:

- **Publisher eligibility**: verified publisher, Partner Center účet +
  business verification, "own or control the MCP server endpoint" (Agnes jako
  operátor vlastní/kontroluje instanci → splnitelné)
- **Package**: manifest JSON (`agentConnectors[].toolSource.remoteMcpServer.mcpServerUrl`
  + `mcpToolDescription` soubor) + `intro.md` + ikony + privacy/terms linky
- **Auth**: `authorization.type: "AzureKeyVault"` — client id/secret/token URL
  se čtou z **Azure Key Vault** referencí v manifestu, ne z dynamic
  registration. **Nová nesrovnalost k prověření**: nejasné, jak/jestli tohle
  spolupracuje s Agnes's OAuth 2.1 + PKCE + RFC 8414 dynamic discovery model
  — vypadá to na statickou registraci per-submission, podobně jako Gemini
  Enterprise (CON-5). Netestováno, jen zjištěno z dokumentace.
- Proces: automated validation → functional/safety review → publish do
  Copilot Studio **i Azure Foundry** současně.

**Doporučení**: cílit na tuhle MCP-specific cestu, ne na starší Power
Platform connector certification — je to ten skutečný, dnešní MCP ekvivalent
Claude/OpenAI directory review, a nemá open-source podmínku. Zbývá ověřit
Key Vault/DCR nesrovnalost a Partner Center business-verification bariéru
(otázka 3 výše).
