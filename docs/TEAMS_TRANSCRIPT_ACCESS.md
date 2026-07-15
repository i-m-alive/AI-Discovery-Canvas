# Teams Transcript Import — Access Diagnosis & Fix

Why importing a transcript works for meetings you **organized** but can fail for
meetings you were only **invited** to, what the current Azure permissions cover,
and the exact admin steps that enable the remaining cases.

**App registration:** NaviCORE · client id `af5c62ae-ca02-4053-83b3-96391f2ff1a9`
· tenant `93c825a6-ff29-4db8-bf65-f369fa31d7fe` (Navikenz)
· owner: Vanshita Mediratta
**Verified against:** the Azure portal *API permissions* blades (2026-07-15) and
`backend/app/services/graph_teams.py` (the module's behavior notes were confirmed
live against Graph, not assumed).

---

## 1. Current state — what is already granted

All admin-consented, all **Delegated** type:

| Permission | Used for | Needed? |
|---|---|---|
| `Calendars.Read` | Browse your meetings in the import modal | ✅ required |
| `OnlineMeetings.Read` | Resolve a meeting from its join URL | ✅ required |
| `OnlineMeetingTranscript.Read.All` | List + download transcripts | ✅ required |
| `offline_access` | Refresh token → connection survives backend restarts | ✅ required |
| `User.Read` | Sign-in / profile | ✅ required |
| `Calendars.ReadBasic`, `Files.Read.All`, `Sites.Read.All` | Not used by transcript import | harmless extras |

> The *User consent* tab listing only 3 individual users is irrelevant:
> **admin consent applies tenant-wide**, so every Navikenz account (including
> kumarshivraj.bhakat@navikenz.com) is covered.

**The delegated permission set is complete.** Nothing is missing for the app's
primary path.

## 2. What works today

- Browse calendar meetings, per-meeting **transcript availability badge**.
- Import transcripts for meetings **you organized**.
- Import transcripts for **most meetings you were invited to** — the backend
  constructs the Graph meeting id locally from the join URL and uses
  `GET /me/onlineMeetings/{id}`, which (unlike the `$filter` search) accepts an
  *attendee's* delegated token.

## 3. Why some invited-meeting imports still fail

Three failure modes survive correct delegated permissions — these are
Microsoft Graph restrictions, confirmed live, not app bugs:

1. **Shared-mailbox organizer** — the meeting was organized by a
   shared/distribution mailbox (e.g. `team-all@…`), not a real user. Graph
   returns error `3003: User does not have access to lookup meeting`.
2. **Recurring-meeting occurrence** — a specific occurrence whose Graph-side
   attendance record doesn't include you, even though it's on your calendar.
   A sibling occurrence of the same series can import fine.
3. **Content gating** — transcript *listing* succeeds for an attendee but
   downloading the transcript *bytes* 403s; Graph gates content more strictly
   than metadata under delegated auth.

All three are handled by the app's built-in **organizer fallback**: it retries
the lookup + download under the *organizer's* identity using app-only
(client-credentials) auth. That fallback is currently a **no-op** because the
three prerequisites below are missing.

## 4. The fix — three admin steps (no code changes)

> Step 1 turns the app registration into a *confidential client* — a real
> security-posture change the app owner should knowingly approve.
> Steps 2–3 need a Global / Application Administrator.

1. **Client secret**
   Azure Portal → App registrations → NaviCORE → *Certificates & secrets* →
   New client secret. Put the secret **value** in `backend/.env`:
   ```ini
   TEAMS_CLIENT_SECRET=<the value, not the secret id>
   ```
   (or Key Vault secret `teams-client-secret`). Restart the backend.

2. **Application permissions** (not Delegated)
   NaviCORE → *API permissions* → Add a permission → Microsoft Graph →
   **Application permissions** → add:
   - `OnlineMeetings.Read.All`
   - `OnlineMeetingTranscript.Read.All`

   Then click **Grant admin consent for Navikenz**.

3. **Teams application access policy**
   App-only access to online meetings additionally requires a policy, granted
   via Teams PowerShell by a Teams admin:
   ```powershell
   Connect-MicrosoftTeams
   New-CsApplicationAccessPolicy -Identity "NaviBA-Transcripts" `
     -AppIds "af5c62ae-ca02-4053-83b3-96391f2ff1a9" `
     -Description "AI Discovery Canvas transcript import (organizer fallback)"

   # grant per organizer…
   Grant-CsApplicationAccessPolicy -PolicyName "NaviBA-Transcripts" -Identity <organizer-upn>
   # …or tenant-wide
   Grant-CsApplicationAccessPolicy -PolicyName "NaviBA-Transcripts" -Global
   ```
   Policy changes can take up to ~30 minutes to propagate.

Once all three are in place, `graph_teams.is_app_only_configured()` flips true
and the fallback activates **automatically** on exactly the meetings that fail
today. Nothing else to configure in the app.

## 5. Non-permission checklist (check these first)

- **Was transcription turned on during the meeting?** No transcript in Teams =
  nothing to import — the modal's availability check shows a "No transcript"
  badge for this case. No permission fixes this.
- Retention policies can delete transcripts after a period.
- The meeting must be a Teams *online* meeting reachable from your calendar
  (the join URL is what the lookup is built from).
- Channel meetings and externally-organized (other-tenant) meetings are out of
  scope for both the delegated path and the fallback.

## 6. Quick reference — which path handles what

| Scenario | Path | Works today? |
|---|---|---|
| You organized the meeting | Delegated | ✅ |
| Invited, regular meeting, real-user organizer | Delegated (constructed-id) | ✅ mostly |
| Invited, shared-mailbox organizer | Organizer fallback (app-only) | ❌ until §4 done |
| Invited, recurring occurrence w/ missing attendance record | Organizer fallback | ❌ until §4 done |
| Attendee metadata OK but content download 403s | Organizer fallback | ❌ until §4 done |
| Transcription was never enabled | — | ❌ impossible by design |
