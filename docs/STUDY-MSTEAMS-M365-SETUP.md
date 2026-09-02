# Study Guide — Connecting the Habit Assistant to Microsoft Teams (the M365 / Azure side)

> **What this is.** A teaching document, not a build spec. It explains *everything you (the tenant owner) would have to do on the Microsoft 365 / Azure side* to let our habit-tracking assistant talk to Microsoft Teams — **before** you decide whether to commit to a build. No code here. Read it, understand the shape of the work and the cost, then decide.
>
> **Who does what.** Almost every step below is an **admin action only you can perform** (they need your tenant's Global Admin / Teams Admin rights). The engineering team can only start once you hand them ~6 values that these steps produce.
>
> **Audience.** You run the Thai company tenant `ngowhock.co.th`. English is primary; key terms carry a Thai gloss in *(ภาษาไทย)* where it helps.
>
> **Status:** Phase-1 research. Portal names verified September 2026. Anything I could not verify from outside your tenant is marked **“verify in portal — may have moved.”**

---

## 0. The one-sentence summary

A Teams bot is **not** a direct webhook like LINE or Telegram. Microsoft insists that an **Azure Bot resource** sit in the middle: Teams talks to Microsoft's *Bot Framework Service*, and that service relays messages to our server over HTTPS. So the setup is “register an identity → create the Azure Bot → point it at our server → switch on the Teams channel → package a tiny Teams app and let your org install it.” Roughly **10 admin steps**, mostly one-time, and it can run **for free** on hardware you already own.

---

## Part 1 — The Big Picture: how a Teams bot actually connects

### 1.1 The five pieces (all live in *your* tenant)

Think of it as five Lego bricks. You build them once.

| # | Piece | Thai gloss | What it is | Why it exists |
|---|---|---|---|---|
| 1 | **Entra ID app registration** | *การลงทะเบียนแอป* | An identity + password for the bot in Microsoft Entra ID (the old “Azure AD”). Produces an **App ID** (client ID) and a **client secret**. | This is *who the bot is* when it proves itself to Microsoft. |
| 2 | **Azure Bot resource** | *ทรัพยากรบอทบน Azure* | A small object you create in the Azure portal that links the app identity (#1) to the Bot Framework. | This is the **hub** Microsoft requires. Teams will only speak to a registered Azure Bot, never to a raw URL. |
| 3 | **Messaging endpoint** | *ปลายทางรับข้อความ* | A public HTTPS URL on **our** server that receives messages. **Our Tailscale Funnel URL fits here perfectly** (e.g. `https://notiserver.tail6ea7.ts.net/api/messages`). | This is where Microsoft *delivers* the incoming messages. It is the only piece that points back at us. |
| 4 | **Teams channel toggle** | *การเปิดช่อง Teams* | A single switch on the Azure Bot: “connect this bot to Microsoft Teams.” | Turns a generic bot into a Teams bot. One click. |
| 5 | **Teams app package** | *แพ็กเกจแอป Teams* | A tiny `.zip` = `manifest.json` (a description file) + two PNG icons. | This is what actually *appears inside Teams* so a person can open a chat with the bot. Without it, the bot exists but no one can find it. |

> **Mental model:** #1 and #2 are the bot’s *passport and hub*. #3 is *our house* where mail is delivered. #4 says *“accept Teams mail.”* #5 is the *business card* your staff use to start a conversation.

### 1.2 How a message actually flows

**Incoming (person → us):**

```
Person types "500ml" in Teams
        │
        ▼
Teams client
        │
        ▼
Bot Framework Service   ← Microsoft's cloud, part of the Azure Bot (#2)
        │  HTTPS POST, carrying a signed JWT token
        ▼
OUR messaging endpoint (#3)  →  our aiohttp webhook validates the token,
                                 then runs the same core/ habit logic as today
```

**Reply (us → person):** we do **not** just answer the HTTP request. We call Microsoft back:

```
Our code builds a reply "Activity" (JSON)
        │  needs an OAuth2 access token first
        ▼
We fetch a token from login.microsoftonline.com
   (using App ID + client secret; audience = Bot Framework)
        │
        ▼
POST the reply to the "serviceUrl" Microsoft gave us
        │
        ▼
Bot Framework Service  →  Teams client  →  Person sees the reply
```

**Proactive (reminders / check-ins, us → person with no incoming message):** identical to a reply, except we use a **stored “conversation reference”** (a small bundle: serviceUrl + conversation id + user id + tenant id + channel id) that we captured the first time the person messaged us. **No special permission is needed** for this — more on that in Step 10.

### 1.3 Why this is different from LINE (and Telegram)

Our LINE edition (`SPEC-LINE.md`) is the closest thing we have to a template — it is also a webhook server behind Tailscale Funnel — but Teams is meaningfully heavier in a few places and *lighter* in others. Here is the honest contrast:

| Concern | **LINE** (our live template) | **Microsoft Teams** |
|---|---|---|
| Who delivers the message | LINE POSTs **straight to our webhook** | Teams → **Bot Framework Service** → our webhook (extra hop, extra cloud object to create) |
| Proving the message is genuine | HMAC-SHA256 over the raw body using a **shared channel secret** (`x-line-signature`) | A **signed JWT** in the `Authorization` header that we must validate against Microsoft's public keys + our App ID |
| Sending a reply | `POST api.line.me` with a **static bearer token**; a free `replyToken` | `POST` to a **dynamic serviceUrl** with an **OAuth2 token we must fetch & refresh** (~24 h) using App ID + secret |
| Auth model | One long-lived channel access token | App registration + client secret + short-lived OAuth tokens |
| Sending an image | Needs a **public HTTPS media URL** — we serve tokened PNGs via Funnel | **Inline base64 data URL is accepted** — *no media server needed* (see §4) |
| Editing a sent message | **Not possible** — live dashboard degraded to re-sends | **Possible** via `updateActivity` — the live dashboard can work again |
| Proactive send cost | **Counts against a ~300/month push quota** on the free plan | **Free** — no per-message fee on standard channels |
| “Install / discover” the bot | Rich menu + add the Official Account | Upload a small app package to the org’s app catalog |

**Bottom line:** Teams costs you *more setup ceremony* (an Azure subscription, an app registration, an Azure Bot, an app package) but *fewer product compromises* (editing works, images are simpler, proactive is free and unlimited). It is the richer channel of the three.

---

## Part 2 — What you must do in M365 / Azure, step by step

Ten steps. I mark each **[YOU ONLY]** (needs your admin rights) or **[team can do]**. Estimated one-time effort is in each heading.

### Step 1 — Reality-check: do you even have an Azure subscription? **[YOU ONLY]** *(~15 min, possibly a card)*

**The trap most people hit first:** *An M365 tenant does **not** automatically come with an Azure subscription.* You have Entra ID (the directory) and M365 licenses, but an **Azure Bot resource is an Azure resource and needs an Azure *subscription* to live in** — even on the free F0 tier, even though the bot itself is free.

- A **subscription** *(การสมัครใช้บริการ Azure)* is a billing container. Your tenant probably has **zero** today.
- **What to do:** sign in to <https://portal.azure.com> with a tenant admin account → search “Subscriptions” → **Add**. Choose either:
  - **Azure free account** — includes a credit for the first 30 days and a set of always-free services; requires a credit card for identity verification but is not charged for F0 bot usage, **or**
  - **Pay-as-you-go** — no upfront cost; you are billed only for what you use. Since our bot uses only the **free F0 tier and your own VPS for compute**, the expected Azure bill is **฿0/month** (see Part 3).
- The subscription is created **inside your `ngowhock.co.th` tenant**, so the Azure Bot and the app registration share one home tenant (Microsoft requires this).

> **Decision for you:** which account holds the subscription and pays if anything ever bills? A pay-as-you-go subscription with a spending cap alert is the safe choice for a private company bot.

### Step 2 — Register the bot's identity in Entra **[YOU ONLY]** *(~10 min)*

Go to the **Microsoft Entra admin center** <https://entra.microsoft.com> → **Identity → Applications → App registrations → New registration**. *(This is the tool formerly called “Azure Active Directory → App registrations”; the old name still appears in some docs — verify in portal, may have moved.)*

- **Name:** something like `Habit Assistant Bot`.
- **Supported account types** *(ประเภทบัญชีที่รองรับ)* — **choose “Accounts in this organizational directory only (Single tenant)”**.
  - **Why single-tenant:** this is a private company/family bot; no outsider should ever be able to install it. Single-tenant means “only `ngowhock.co.th` accounts.”
  - **Also important:** Microsoft has **deprecated the multi-tenant bot type** for *new* Azure Bot registrations — single-tenant (or a managed identity) is now the required/normal choice anyway. So single-tenant is both the safest *and* the current-supported path.
- Leave “Redirect URI” blank (a bot doesn’t need one for basic messaging).
- After it’s created, copy two values from the **Overview** page:
  - **Application (client) ID** — this is your **App ID**.
  - **Directory (tenant) ID** — your **Tenant ID** (for `ngowhock.co.th` this is already known — see the Appendix).

### Step 3 — Create the client secret (the bot's password) **[YOU ONLY]** *(~5 min — and a calendar reminder)*

Still in the app registration → **Certificates & secrets → Client secrets → New client secret**.

- Give it a description (`bot secret 2026`) and an expiry.
- **Hard limit:** the portal will **not** let you pick longer than **24 months** — Microsoft capped client-secret lifetime at 2 years. Microsoft’s own security guidance is stricter (they recommend **≤ 6 months** and rotating regularly).
- **Copy the secret *Value* immediately.** It is shown **once**. If you navigate away you cannot retrieve it — you’d have to delete it and make a new one.
- **The rotation caveat (write it down):** when this secret expires, **the bot silently stops being able to reply** (error `AADSTS7000222`). Nothing warns you in the chat. **Put a calendar reminder ~2 weeks before the expiry date** to generate a new secret and hand it to the team.

> **Recommendation:** set the expiry to **12 months** as a balance between Microsoft’s 6-month ideal and the annoyance of rotating, and put the renewal date in a shared calendar the day you create it.

### Step 4 — Create the Azure Bot resource **[YOU ONLY]** *(~10 min)*

Back in the **Azure portal** <https://portal.azure.com> → **Create a resource** → search **“Azure Bot”** → **Create**.

- **Bot handle:** a unique name, e.g. `habit-assistant-teams`.
- **Subscription / Resource group:** the subscription from Step 1; make a resource group like `rg-habit-bot`.
- **Pricing tier:** **choose F0 (Free).** *(pricing honesty in Part 3.)* Do **not** pick S1 unless you have a specific reason — F0 is more than enough for this scale.
- **Type of App:** **Single-Tenant** (matches Step 2).
- **Existing app registration:** select **“Use existing app registration”** and paste the **App ID** (Step 2) and the **Tenant ID**. *(You are linking the identity you already made, not creating a new one.)*
- Click **Review + create**.

> **Why reuse the Step-2 registration** rather than let Azure auto-create one: you keep the identity and its secret in one place you control, and it makes single-tenant configuration explicit.

### Step 5 — Point the bot at our server (the messaging endpoint) **[team supplies the URL, YOU paste it]** *(~5 min)*

In the new Azure Bot → **Settings → Configuration** → **Messaging endpoint**.

- Paste the HTTPS URL of **our** webhook. It will look like:
  `https://notiserver.tail6ea7.ts.net/api/messages`
  (the same Tailscale Funnel host already serving the LINE bot; the path — `/api/messages` — is the Bot Framework convention, but it is ours to choose).
- **Requirements Microsoft enforces on this URL:**
  - Must be **HTTPS with a valid certificate** — Funnel’s `ts.net` certificate satisfies this (it already does for LINE).
  - Must be **publicly reachable** — no `localhost`, no IP-only.
  - Must **answer within ~15 seconds** — see the Gotchas. (Our LINE design already returns `200` immediately and processes asynchronously; the Teams server will do the same.)
- On this same **Configuration** page, confirm **App Type = Single Tenant** and that the **App Tenant ID** field is filled — a single-tenant bot that is missing its tenant ID **fails authentication silently** (Gotcha #4).

### Step 6 — Switch on the Teams channel **[YOU ONLY]** *(~2 min)*

In the Azure Bot → **Settings → Channels** → select **Microsoft Teams** → accept the terms → **Apply**.

- This is the whole step. Teams and Web Chat are **standard channels** and are **free**.
- (Ignore Direct Line, WhatsApp, Telegram, etc. here — each is a separate channel; you only need Teams. Direct Line is a *premium* channel and would cost money — don’t enable it.)

### Step 7 — Open the tenant gate: allow custom app upload **[YOU ONLY — the step people get stuck on]** *(~10 min, allow time for propagation)*

This is the **single most common blocker.** By default many tenants **forbid** staff from installing “custom” (a.k.a. line-of-business / sideloaded) apps, so even after everything above works, **nobody can add the bot**. You must open two-to-three switches in the **Teams admin center** <https://admin.teams.microsoft.com>:

1. **Org-wide app settings** — **Teams apps → Manage apps → Org-wide app settings** (a button near the top right). Ensure custom/third-party apps are **allowed**. *(The exact label has been renamed over time — you may see “Custom apps,” “Let users interact with custom apps,” or “…in preview.” Turn the custom-app interaction setting **On**. Verify in portal — may have moved.)*
2. **App setup policy** — **Teams apps → Setup policies → Global (Org-wide default)** → set **“Upload custom apps” = On**. This is what actually lets an assigned user sideload the package. You can leave it global, or scope a **custom policy** to just yourself + the people who’ll use the bot (see Step 9).
3. *(If you later install into a specific team rather than personal chat)* a **team-level** setting also governs uploads; not needed for a personal-chat bot.

> There are genuinely **three layers** of control (org-wide setting, setup policy, team setting). All relevant ones must be **On** or the upload in Step 8 fails with a permissions error. Changes can take **a few minutes to a few hours** to propagate.

### Step 8 — Build & upload the Teams app package **[team builds the package; YOU or team upload]** *(package: team ~1 hr; upload: ~5 min)*

The **app package** is a `.zip` containing exactly three files:

- `manifest.json` — a description: the bot’s App ID (from Step 2), name, which “scope” it runs in (personal chat vs team channel), commands list, etc. *(The team writes this — it’s tiny, ~40 lines.)*
- `color.png` — a 192×192 full-colour icon.
- `outline.png` — a 32×32 transparent outline icon.

**Two ways to get it into Teams — the easy modern path is the Developer Portal:**

- **Developer Portal for Teams** <https://dev.teams.microsoft.com> → **Apps → Import app** (upload the `.zip`) → review → **Publish → Publish to your org**. This is the recommended, guided route; it can also *generate/validate* the manifest for you. *(Verify in portal — the Developer Portal has replaced the old “App Studio.”)*
- **or** Teams admin center → **Teams apps → Manage apps → Upload new app** (upload the same `.zip`) — the classic admin route.
- **or** for a quick private test, inside the Teams client itself: **Apps → Manage your apps → Upload a custom app** (only works if Step 7 enabled it).

> **Publishing to your org** puts the bot in your tenant’s **app catalog** so approved staff can add it. It does **not** submit anything to Microsoft’s public store — see Gotcha #5. Uploading to the Developer Portal alone does **not** distribute it; you must **Publish to org** (or hand people the `.zip`).

### Step 9 — Scope the bot to specific people **[YOU ONLY]** *(~5 min, optional but recommended)*

For a private company/family bot you probably don’t want *everyone* in `ngowhock.co.th` to see it.

- After the app is in **Manage apps**, open it and set **availability** / use an **app permission policy** to allow only chosen users or a group.
- Or scope the **App setup policy** (Step 7.2) so only a pilot group can upload/see it.
- Our own product already has an **owner allowlist + onboarding gate** (v1.2 multi-user access control), so even if a curious colleague adds the bot, they land in a “pending approval” state until you approve them. **Teams scoping + our in-app gate are belt-and-suspenders.**

### Step 10 — Permissions & consent: what this bot actually needs **[YOU ONLY, if anything]** *(~5 min — usually nothing)*

This is where people over-provision out of fear. The precise truth:

| Capability | Microsoft Graph permission needed? | Admin consent needed? |
|---|---|---|
| **Receive messages** a person sends the bot | **None.** The Bot Framework channel handles it. | No |
| **Reply** to a person | **None.** Uses the bot’s own OAuth token (App ID + secret). | No |
| **Proactive messages** — reminders, check-ins, nudges — to a person **who has already added/messaged the bot** | **None.** We reuse a stored *conversation reference*. This is exactly how our reminders will work. | No |
| **Proactively *install* the bot** for someone who has **never** added it (push it into their Teams silently) | Yes — `TeamsAppInstallation.ReadWriteForUser.All` | **Yes, admin consent** |
| Reading a person’s calendar/mail/roster, org-wide messaging, etc. | Various Graph scopes | Usually yes |

**For our habit assistant, the answer is: no Graph permissions and no admin-consent grant are required.** Every proactive message we send (the daily reminder, the nudge, the check-in) goes to a user **who has already started a chat with the bot**, so a stored conversation reference is all we need. We deliberately **do not** want the “proactive install” capability (that’s the only thing that would need your admin consent, and it’s overkill for a bot people opt into).

> If a future feature ever wants to read Teams presence, calendars, or install the bot for people automatically, *that* is when we’d come back to you for a consent grant. Not now.

---

## Part 3 — Costs, itemised honestly

| Item | Tier / source | Cost |
|---|---|---|
| **Azure Bot resource** | **F0 (Free)** — includes 10,000 messages/month on standard channels | **฿0** |
| **Teams channel** | Standard channel | **฿0** (free) |
| **Per-message fees** | Standard channels have none | **฿0** |
| **Proactive messages** (reminders/check-ins/nudges) | Free on standard channels — **no quota** | **฿0** (contrast: LINE free plan caps ~300 pushes/month total) |
| **Bot compute / hosting** | Runs on **our existing VPS** (`notiserver`, the same box as the LINE bot) behind Tailscale Funnel | **฿0 extra** — no Azure App Service, no VM to rent |
| **Azure subscription itself** | Pay-as-you-go with no resources billed | **฿0** as long as you stay on F0 and add nothing else |
| **Entra app registration & client secret** | Part of Entra ID (already in your M365) | **฿0** |
| **Teams app / “store” review** | Not required for org-internal custom apps | **฿0** |

**Expected monthly cost: ฿0.** The habit assistant on Teams is genuinely free at your scale.

**The *only* ways this accidentally costs money:**
1. Choosing **S1** instead of F0 when creating the Azure Bot (S1 is pay-per-message; unnecessary here).
2. Enabling a **premium channel** like **Direct Line** (billed per 1,000 messages) — you don’t need it; Teams is a standard channel.
3. Letting the team put the bot compute on **Azure App Service / a VM** instead of our VPS — that would be a hosting bill we can entirely avoid by reusing `notiserver`.
4. Adding other Azure resources (App Insights beyond the free grant, storage, etc.). Keep the resource group to just the F0 Azure Bot.

> **Guardrail:** after Step 1, set a **budget alert** on the subscription at, say, ฿100/month. If it ever fires, something was misconfigured — because the correct configuration bills nothing.

---

## Part 4 — Feature mapping: what THIS product can do on Teams

Because Teams is richer than LINE, several features we *degraded* for LINE come **back to full strength** on Teams. Here’s the mapping for our actual feature set.

| Our feature | Telegram (today) | LINE (degraded) | **Teams (what you’d get)** |
|---|---|---|---|
| **Live pinned dashboard** (edit-in-place) | ✅ edit + pin | ❌ re-send only (no edit) | ✅ **Works** — Bot Framework `updateActivity` edits a message the bot sent (cache the activity id). *(Pinning in a chat is more limited than Telegram’s, but the edit-in-place refresh — the important half — is available.)* |
| **Quick-log buttons / menus** | Inline keyboard | Quick replies (≤13) + rich menu | ✅ **Richer** — **Adaptive Cards** replace both. Buttons, input fields, structured layouts, all in one card. |
| **Images** (heatmap, `/wrapped`, weekly chart PNGs) | `sendPhoto` (bytes) | ❌ needs a **public media URL** (we serve tokened PNGs via Funnel) | ✅ **Simpler than LINE** — Bot Framework accepts an **inline base64 data URL** (`contentUrl: "data:image/png;base64,…"`), or an Adaptive Card `Image` element. **No public-URL dance, no media server** — our in-memory PNG bytes attach directly. |
| **Proactive reminders / check-ins / nudges** | ✅ free, unlimited | ⚠️ **collapsed into ONE daily digest** (push quota) | ✅ **Free and unlimited again** — Telegram-style behaviour returns. Hourly check-ins, per-time reminders, the almost-there nudge can all fire independently. Uses a stored conversation reference; no quota, no digest compromise. |
| **Emoji reactions on a log** | ✅ | ❌ no-op | ⚠️ Partial — Teams bots have limited reaction APIs; treat as a nice-to-have, likely degrade to a ✅ text confirm. **Verify at build time.** |
| **Command menu** | `setMyCommands` | Rich menu | ✅ Manifest `commands` list + Adaptive Card actions; also `/`-style commands. |
| **Chat scope** | 1:1 | 1:1 | **Choice:** personal chat *(แชทส่วนตัว)* — recommended, mirrors today’s 1:1 model — **or** a team channel (group visibility). Threads exist in channels; personal scope keeps it simple and private. |

**Headline wins vs LINE:** the **live dashboard works**, **images are easier** (no media server), and **proactive messaging is free and unlimited** (no digest collapse). Teams is, feature-for-feature, the *closest to the full Telegram experience* of the two alternative channels — with Adaptive Cards it can even exceed it visually.

**One thing to decide:** **personal chat vs team channel.** For a habit tracker, **personal (1:1) scope** matches how the bot works today and keeps each person’s data private in their own chat. Team-channel scope is only worth it if you want shared/visible group habits.

---

## Part 5 — The checklist: what to have ready before asking the team to build

### 5.1 The ~6 values the build needs (produced by the steps above)

| # | Value | From step | Sensitivity |
|---|---|---|---|
| 1 | **App (client) ID** — a GUID | Step 2 | Not secret (identifies the app) |
| 2 | **Client secret** — a long string | Step 3 | **SECRET** — hand over securely, never commit to git |
| 3 | **Tenant ID** — a GUID | Step 2 (Appendix has yours) | Not secret |
| 4 | **Messaging endpoint URL** — our Funnel HTTPS + `/api/messages` | Step 5 (team provides) | Not secret |
| 5 | **App type = Single-Tenant** (a setting, not a value, but the team must know it) | Steps 2 & 4 | — |
| 6 | **The app package `.zip`** (manifest + 2 icons) | Step 8 (team builds, you publish) | Not secret |

> Items 1–3 map exactly onto our existing secret pattern — compare LINE’s `.env` (`LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `LINE_OWNER_USER_ID`). The Teams equivalent would be roughly `MICROSOFT_APP_ID`, `MICROSOFT_APP_PASSWORD` (the secret), `MICROSOFT_APP_TENANT_ID`, plus an owner user id captured at first `/start`.

### 5.2 The admin-only actions (only YOU can do these)

- [ ] **Step 1** — Create an Azure subscription in the tenant *(no one else can; needs tenant admin + billing)*
- [ ] **Step 2** — Register the Entra app (single-tenant)
- [ ] **Step 3** — Create the client secret + set a renewal calendar reminder
- [ ] **Step 4** — Create the Azure Bot resource (F0, single-tenant, reuse the app registration)
- [ ] **Step 5** — Paste the messaging endpoint URL (team gives you the URL)
- [ ] **Step 6** — Enable the Microsoft Teams channel
- [ ] **Step 7** — Open the tenant gate: allow custom apps (org-wide setting + setup policy “Upload custom apps = On”)
- [ ] **Step 8** — Publish the app package to your org (Developer Portal → Publish to org, or admin center upload)
- [ ] **Step 9** — Scope the app to chosen users / group
- [ ] **Step 10** — Confirm no Graph consent is needed (it isn’t, for our features) — and decline any request for `TeamsAppInstallation.*` unless we explicitly justify it later

### 5.3 What the *team* does (so you know the division of labour)

- Write the `manifest.json` + icons (Step 8 package).
- Provide the messaging-endpoint URL (Step 5), i.e. stand up the Teams webhook on `notiserver`.
- Build the Teams channel adapter behind our existing `channels.base.Channel` seam (the same seam LINE plugged into — `core/` and `storage/` stay untouched).
- Validate the inbound JWT, fetch/refresh the OAuth token, store conversation references for proactive sends.

---

## Part 6 — Gotchas (read these before you start clicking)

1. **No subscription = a hard wall.** You *will* hit “you need a subscription” at Step 4 if you skip Step 1. It surprises everyone: having M365 is not having Azure. *(มี M365 ไม่ได้แปลว่ามี Azure subscription.)*
2. **The client secret expires — max 24 months — and its death is silent.** When it lapses the bot just stops replying (`AADSTS7000222`). **Calendar reminder ~2 weeks before expiry.** Prefer a 6–12 month expiry and rotate.
3. **The 15-second rule.** Your messaging endpoint must return an HTTP `200` within ~10–15 seconds or Teams shows “something went wrong.” The fix is architectural and we already do it on LINE: **answer `200` immediately, process asynchronously.** Also: reply to Teams’ system events with `200`, never `5xx` — repeated `5xx` makes the channel back off.
4. **Single-tenant needs the tenant ID *everywhere*.** A single-tenant bot authenticates against your specific tenant authority. If the Azure Bot config or the app settings are missing the **App Tenant ID**, authentication fails **silently** (works in the test Web Chat, dead in Teams). Make sure App ID **and** Tenant ID **and** App Type=Single-Tenant are all set in three places: the app registration, the Azure Bot, and our server config.
5. **No Microsoft store review for an internal custom app.** Sideloaded / “publish to your org” apps are **not** reviewed by Microsoft and need no certification. Review only applies if you ever wanted to list the bot on the **public** Teams store — which we don’t. So there’s **no approval wait** from Microsoft; the only gate is *your own* Teams admin settings (Step 7).
6. **The Developer Portal is the shortcut.** <https://dev.teams.microsoft.com> can generate and validate the manifest, register the bot, and publish to your org from one place — much friendlier than hand-editing JSON. Use it. *(It replaced the old “App Studio.”)*
7. **“Upload custom apps” has three switches, not one** (org-wide setting, app setup policy, team setting). If the upload fails with a permissions error, it’s almost always one of these still Off, or propagation lag (wait a bit).
8. **Portal names drift.** “Azure AD” → “Microsoft Entra ID”; “App Studio” → “Developer Portal”; some Teams-admin labels get renamed release to release. Where a label below doesn’t match what you see, look for the nearest equivalent and treat my path as approximate — **verify in portal**.

---

## Part 7 — FAQ

**Q: Can we reuse the same VPS and Funnel URL as the LINE bot?**
A: Yes. `notiserver` already provides a public HTTPS Funnel endpoint with a valid certificate — exactly what the messaging endpoint needs. The Teams webhook would be a second route/port on the same box. No new hardware, no Azure hosting.

**Q: Do I have to keep my credit card on Azure forever?**
A: A card (or equivalent) is needed to *create* the subscription for identity verification. With F0 + our-own-hosting, nothing bills. Set a budget alert so you’re told the instant anything unexpected appears.

**Q: Will colleagues be able to find/use the bot without me approving them?**
A: Two gates protect you. Teams-side, you scope who can see the app (Step 9). Product-side, our built-in access control (v1.2) holds any new user in “pending approval” until you approve. So no.

**Q: Is the daily-digest compromise from LINE needed on Teams?**
A: No. Teams proactive messages are free and unlimited, so reminders, check-ins, and nudges can each fire on their own schedule — the Telegram-style behaviour. The digest was purely a LINE push-quota workaround.

**Q: How hard is the build, roughly, compared to LINE?**
A: Similar shape (a webhook server behind Funnel, plugged into the same `Channel` seam), with three extra chunks of work: (1) validating Microsoft’s JWT on inbound instead of a simple HMAC, (2) fetching/refreshing an OAuth token for every outbound call, and (3) storing conversation references for proactive sends. The *product* side (`core/`, `storage/`) is untouched, just like LINE. This guide is only about your admin side; a separate build spec would size the engineering.

**Q: Single-tenant vs multi-tenant — am I locking myself in?**
A: Single-tenant is correct for a private bot and is also the current Microsoft-supported default (multi-tenant bot type is deprecated for new registrations). If you ever needed to serve an outside organisation, that’s a different, larger project.

---

## Appendix — Your tenant's known values (pre-filled from public/your-own data)

I was able to confirm a few of your specifics from outside, so you don’t have to hunt for them:

| Value | Yours | How confirmed |
|---|---|---|
| **Tenant primary domain** | `ngowhock.co.th` | Your M365 profile |
| **Tenant ID** *(Directory ID)* | `d3ee60ee-211e-4bf6-ad56-8a05feeb2609` | Public Entra OpenID discovery endpoint for your domain — this is value #3 in the checklist, already in hand |
| **Signed-in developer account** | `Nuttapong@ngowhock.co.th` (Nuttapong Seablaongiw, “Supervisor App Developer”) | Your connected M365 account |
| **Notification account named in the brief** | `nhg-notice@ngowhock.co.th` | From the task — see open question below |

> The Tenant ID above is **not a secret** (it’s discoverable for any Entra domain) — it just saves you a lookup. Everything else (App ID, client secret, Azure Bot) you create fresh in Steps 2–4.

---

## Open questions I could not resolve from outside your tenant

These need *you* (inside the tenant) to check — none of them block understanding the plan, but they shape the exact clicks:

1. **Which account has the admin rights to do all this?** The connected account is `Nuttapong@ngowhock.co.th` (a developer); the brief names `nhg-notice@ngowhock.co.th` as the owner account. **Steps 1–9 need Global Admin (or a mix of Teams Admin + subscription owner).** Confirm which of these two accounts — or a third IT-admin account — actually holds those roles. If Nuttapong isn’t an admin, you’ll need whoever is.
2. **Does the tenant already have *any* Azure subscription?** I can’t see billing from outside. If one already exists (some tenants set up a “pay-as-you-go” long ago), you skip Step 1 and just create the resource group. If not, Step 1 is mandatory and needs a payment method.
3. **Is custom-app upload currently blocked?** Many tenants ship with it Off. I can’t read your Teams admin policies from here. Assume Step 7 is needed until you look; it’s the most likely single point of failure.
4. **Which M365 / Teams license do your users have?** Custom-app sideloading and bot chat are available on standard business plans, but a few very restricted SKUs (some frontline/EDU/government configs) limit it. Very likely fine for a normal Business Standard tenant — worth a glance at your license type.
5. **Personal chat vs team channel** — a product decision, not a blocker: do you want each person’s habits private in a 1:1 chat (recommended, matches today) or visible in a shared team channel?

---

*Prepared as Phase-1 research. No code, no build commitment. When you’re ready to proceed, this checklist’s ~6 values + the admin-only actions are the hand-off to the engineering team.*
