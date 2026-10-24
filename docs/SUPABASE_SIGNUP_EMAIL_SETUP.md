# Email signup and passwordless code setup

The local app supports email/password signup, numeric signup-code verification,
resend, and email editing. The owner saved an SMTP key directly in Supabase.
A fresh loaded dashboard independently confirmed **Custom SMTP enabled**,
`smtp-relay.brevo.com`, port **587**, sender name **CrossFlow AI**, and no unsaved
changes. The Confirm signup template was saved and reloaded with subject
**Verify your CrossFlow account** and a numeric `{{ .Token }}` body.

At 16:13, one actual Supabase Auth `/otp` request for
`2021.ian.abiel.wangsa@ejc.edu.sg` returned HTTP 200 with no session. Its inbox
received a numeric confirmation email from **CrossFlow AI**, subject
**Verify your CrossFlow account**. SMTP delivery to this recipient is verified.
At 16:22, the **Email me a sign-in code** UI requested another numeric
confirmation email and verified it with type `email`. The backend resolved
**CrossFlow OTP Demo** as **driver**, without any agent-set password. EJC's
first passwordless login and restoration after reload are verified. The modal
showed **DRIVER** with `crossflow_profiles` as role source.
[Restored session evidence](verification/otp-login-driver-restored.png).
A repeat code arrived at 16:23 with subject **Your CrossFlow sign-in code**,
and UI verification succeeded again as **driver**, with zero console errors.
Both new-account confirmation and confirmed-account sign-in are verified.
The owner submitted password signup for `ian1234@comp.nus.edu.sg` at 16:26
from `http://127.0.0.1:3000/`, preserving the localhost EJC session. Supabase
created a separate NUS user ID with confirmation sent at **16:26:45 SGT**.
The authorized NUS-only resend at 16:49 was marked Sent/Delivered to the exact
NUS address. The owner then found it in `e1525902@u.nus.edu` Outlook. Codex
independently opened that actual message: its header showed **To:
ian1234@comp.nus.edu.sg**, and the body identified the same recipient. The code
was verified through the original 127.0.0.1 signup screen using `verifySignUpOtp`;
the backend resolved **CrossFlow Driver Demo** as **driver**. **NUS receipt and
signup OTP verification are verified.** The original CrossFlow email remains
`ian1234@comp.nus.edu.sg`; no account email change or merge occurred. Reload
restoration passed; its modal showed **CrossFlow Driver Demo / DRIVER** and
role source `crossflow_profiles`. A separate localhost password sign-in using
the owner-entered password also passed, followed by reload restoration and a
server-resolved DRIVER modal, with zero console errors.
[Restored NUS session](verification/nus-signup-driver-restored.png).
[NUS password-login evidence](verification/nus-password-login-restored.png).
[Exact-recipient events](verification/recipient-specific-smtp-delivery.png),
[NUS verification screen](verification/nus-recipient-bound-verification.png),
[16:49 trace](verification/nus-email-delivery-trace.json).

EJC's 16:22 confirmation and 16:23 sign-in emails were distinct intentional
tests. Its latest confirmation-subject message remains 16:22; no NUS-to-EJC
delivery was observed. The NUS message was instead received in Outlook.
Four email-isolation and three cross-account UI regressions cover the email
binding; explicit recipient notices, resend captions and accessible labels
preserve the current NUS verification stage without changing flow behavior.
Verification submits the requested email together with the code. Numeric
digits are not promised to be globally unique across accounts; use only the
code addressed to the email shown in the current verification screen.

A public MX check returned `84-101.comp.nus.edu.sg` and
`84-102.comp.nus.edu.sg`. These are receiving first hops, not proof of the
final mailbox: Google supports inbound gateways whose domain MX points to
the gateway before mail reaches Gmail. Actual NUS gateway/routing behavior
remains unverified. [Google inbound-gateway documentation](https://support.google.com/a/answer/60730?hl=en).

The user requested Brevo's active **CrossFlow AI v2** key, but the exact saved
key label remains unverified. Supabase masks its stored password and both
Brevo key rows still show `Last used --`; successful delivery does not identify
which key is stored. No secret has been retrieved or copied into this
repository. The older key has not been revoked.

Brevo's verified sender is `ianwangsa2@gmail.com` with display name **CrossFlow
AI**. Phone verification is complete; the freemail warning remains. The
first-class **Email me a sign-in code** flow is implemented and its first live
request/verification succeeded. The full frontend check passed **379 tests in
37 files**, lint, TypeScript and build, including **41 SignInPanel tests** and
**31 email-OTP service tests**. Reload restoration and repeat confirmed-account
verification are verified.

Both templates now identify the recipient with `<strong>{{ .Email }}</strong>`
and say to use the code only with that email. Both updated templates were
independently reloaded: `{{ .Email }}` and `{{ .Token }}` persisted, with Save
disabled. The sign-in subject remains **Your CrossFlow
sign-in code**. Both retain numeric `{{ .Token }}`.
[Recipient-aware signup template](verification/signup-recipient-template-saved.png),
[recipient-aware sign-in template](verification/signin-recipient-template-saved.png).

## Current recommendation: try Brevo Free for the demo

Brevo is the better first attempt while the user has no domain or DNS access.
Its [sender setup](https://help.brevo.com/hc/en-us/articles/208836149-Create-a-new-sender-From-name-and-From-email)
allows verification by a code sent to the sender's inbox, and its
[domain troubleshooting guide](https://help.brevo.com/hc/en-us/articles/16045394674066-Troubleshooting-Domain-authentication)
describes replacing unauthenticated/free sender domains with a Brevo-managed
`brevosend.com` address. The recipient may therefore see that provider domain,
even when the display name is **CrossFlow AI**. The
[Free plan](https://help.brevo.com/hc/en-us/articles/208589409-About-Brevo-s-pricing-plans)
includes transactional email and 300 sends/day.

**This is a conditional demo path, not a guaranteed domain-free service.**
Brevo's [Supabase integration guide](https://developers.brevo.com/docs/supabase-smtp-integration)
lists a DNS-controlled sending domain as a prerequisite for reliable delivery.
Brevo also requires domain authentication before adding a sender whose domain
has DMARC `p=quarantine` or `p=reject`. A university inbox does not provide
control over its DNS or satisfy that requirement. If the account refuses the
chosen sender or requires a domain, record that blocker; do not claim that an
inbox verification bypasses it.

SMTP2GO is a weaker fit here: its
[signup guide](https://support.smtp2go.com/hc/en-gb/articles/12747932085145-Quick-Start-Guide)
requires a work/owned-domain email and excludes shared Gmail/Outlook/Yahoo
addresses. Its [single-sender verification](https://support.smtp2go.com/hc/en-gb/articles/115004408567-Verified-Senders)
uses an inbox link but rejects restrictive-DMARC domains without DNS
authentication. An educational address might pass signup and still fail sender
verification. Neither provider's sender check proves delivery to an educational
recipient; institutional filtering may still defer or quarantine the message.

For a future domain the user owns, Resend's
[Supabase SMTP guide](https://resend.com/docs/send-with-supabase-smtp) is an
alternative requiring a verified sending domain. It is not the current
no-domain setup path.

## Create and verify the Brevo sending identity

1. Account creation, Free-plan sign-in and user-reported phone verification
   are complete; the phone banner/dialog are gone. Complete any further
   provider challenge with truthful account-owner details, without a paid
   upgrade or invented organization, address, domain or phone number.
2. Reuse the existing **Verified** `ianwangsa2@gmail.com` sender with From name
   **CrossFlow AI**. Its display name changed from NUS without changing the
   address or Verified status. No duplicate sender or fresh verification is
   required for this recorded state.
3. Keep the freemail warning visible. Verification establishes the **sending
   identity**, not a CrossFlow user's signup or successful inbox delivery.
   Brevo may apply its documented sender-domain rewrite; no domain
   authentication or delivery guarantee is claimed.
4. Verify that transactional sending is active. A new account can return
   `450 Your SMTP account is not yet activated`; Brevo documents a separate
   [activation requirement](https://help.brevo.com/hc/en-us/articles/115000188150-Troubleshooting-Issues-with-Brevo-SMTP).
   If support activation is required, the account owner must resolve it before
   the demo can claim working delivery.
5. The owner has entered and saved an SMTP key in Supabase, and actual delivery
   succeeded. For a future key change, use **Settings → SMTP & API → SMTP**
   and the actual **SMTP login**, then generate a dedicated standard SMTP key.
   Key creation can require an email/device verification code.
   The full key is shown once; store it securely and enter it only in Supabase's
   SMTP password field. Use an **SMTP key**, not a Brevo API key or account
   password. See [SMTP credentials](https://help.brevo.com/hc/en-us/articles/7924908994450-Send-transactional-emails-using-Brevo-SMTP)
   and [SMTP key creation](https://help.brevo.com/hc/en-us/articles/7959631848850-Create-and-manage-your-SMTP-keys).

Keep provider passwords/keys out of chat, source control, screenshots and
frontend environment files. No SMTP secret or placeholder needs to be added to
the repository's root `.env`: Supabase Auth performs the email sending.

## Configure Supabase

Open this project's
[SMTP settings](https://supabase.com/dashboard/project/wtednggryrikyvkuhqjo/auth/smtp).
The existing sender is Verified. Configure the provider's actual host, port,
username and password, using **CrossFlow AI** as the sender name.

The saved Supabase form contains sender `ianwangsa2@gmail.com`, name
`CrossFlow AI`, host `smtp-relay.brevo.com`, port `587` and the actual Brevo SMTP
login. The owner entered the SMTP password and actual EJC delivery succeeded.
The stored key's label, including whether it is **CrossFlow AI v2**, cannot
be determined from the masked field or the current Brevo usage labels. Do not
put its value in this guide or a frontend environment file.

| Supabase field | Brevo value |
| --- | --- |
| Sender name | `CrossFlow AI` |
| Sender email address | `ianwangsa2@gmail.com` — existing Verified sender |
| SMTP host | `smtp-relay.brevo.com` |
| Port | `587` with STARTTLS; `465` with SSL/TLS is the documented alternative |
| Username | The actual SMTP **Login** from Brevo's SMTP page, which may be an account email or a generated address such as `[ID]@smtp-brevo.com` |
| Password | The dedicated Brevo **SMTP key** |
| Minimum interval per user | `60` seconds |

Do not use the SMTP login or relay hostname as the From email. Save only after
the account, sender and key are valid. Supabase and Brevo enforce separate
quotas, so inspect Supabase's resulting Auth email rate limit as well as Brevo's
300/day free allowance. The port/credential mapping is documented in
[Brevo's Supabase guide](https://developers.brevo.com/docs/supabase-smtp-integration);
the current SMTP login format is documented in
[Brevo SMTP configuration](https://help.brevo.com/hc/en-us/articles/7924908994450-Send-transactional-emails-using-Brevo-SMTP).

Keep Supabase **Confirm Email** enabled. Under Authentication → Email templates
→ Confirm signup, use subject **Verify your CrossFlow account** and this body:

```html
<h2>Verify your CrossFlow account</h2>
<p>This verification is for <strong>{{ .Email }}</strong>.</p>
<p>Enter this verification code in CrossFlow:</p>
<p style="font-size:28px;font-weight:700;letter-spacing:4px">{{ .Token }}</p>
<p>Use this code only with the email address above.</p>
<p>If you did not request this account, you can ignore this email.</p>
```

The password-signup flow calls `/auth/v1/signup`, verifies with type `signup`,
and resends with type `signup`. The passwordless flow requests a Supabase
email OTP and can create a pending account without a password. Both templates
must contain `{{ .Token }}` for numeric entry: **Confirm signup** uses subject
**Verify your CrossFlow account**; **Magic link or OTP** now uses subject
**Your CrossFlow sign-in code**. Both include `{{ .Email }}` and instructions
to use the code only with that email; both latest updates are saved and reload-
verified. The first EJC
request produced the confirmation subject above; it did not establish a session.
The UI accepts 6–10 numeric digits for the configured Supabase token length.

## Verify the actual journey

1. For EJC, use **Email me a sign-in code**. Its first live request and code
   verification succeeded as driver, survived reload and succeeded again for
   the confirmed account. No CrossFlow password
   is required. Numeric emails have already been delivered;
   obtain a fresh code through the authorized UI if the existing code expires.
2. NUS password signup and signup OTP verification succeeded using the 16:49 message received
   in Outlook, addressed to `ian1234@comp.nus.edu.sg`. Keep that canonical
   CrossFlow email; do not substitute the Outlook mailbox address or merge
   accounts. A separate owner-entered password sign-in, reload restoration and
   server-resolved DRIVER modal are also verified, with zero console errors.
3. Confirm actual receipt for each subsequent requested code. The one EJC
   delivery does not prove delivery to another address or after a key change.
4. Enter the code in the app, and verify a successful Supabase session and a
   backend-resolved driver profile. A new account must not become an admin.
5. Check a rejected incorrect/expired code and resend after the displayed
   cooldown. Confirm that the unverified account cannot access account history.
6. Restoration after reload and repeat confirmed-account verification are
   verified for EJC. Check the chosen method separately for each additional
   account; NUS receipt, signup OTP, password sign-in and reload are now verified.

An educational recipient does not need to become a Brevo account member or a
Supabase organization member. Check Brevo's transactional logs for accepted,
delivered, deferred or bounced events, and confirm the actual inbox and
CrossFlow verification result. SMTP acceptance alone is not end-to-end proof.

Record live delivery, verification and role results in
[LOCAL_VERIFICATION_REPORT.md](LOCAL_VERIFICATION_REPORT.md). Record EJC inbox
delivery, first UI code verification, reload restoration and repeat confirmed-
account login as verified. Record NUS Outlook receipt and signup OTP verification
and owner-entered password sign-in/reload as verified. Keep unknown credential
rotation/key-label status distinct from those successful checks.

Supabase's default mailer is restricted to organization-team email addresses
and currently two messages per hour. Do not add test users to the organization
to bypass that restriction. See the official
[SMTP guide](https://supabase.com/docs/guides/auth/auth-smtp).

## NUS delivery trace and handoff

**Resolved: Outlook receipt and NUS signup verification succeeded.** The
trace preserves the earlier negative Gmail searches and now records the
16:49 message found in `e1525902@u.nus.edu` Outlook. Codex independently confirmed the message
header's **To: ian1234@comp.nus.edu.sg** and the recipient-specific body, then
verified its code against the original NUS signup. The backend resolved
**CrossFlow Driver Demo / driver**, and reload restoration passed with
`crossflow_profiles` as role source. No support escalation is needed for this
message, and no support message or ticket was sent. The institutional
forward/gateway explanation is plausible; the full Received chain was not
inspected, so the precise routing rule remains unverified.

The [16:49 trace](verification/nus-email-delivery-trace.json) records one
NUS-only resend with recipient `ian1234@comp.nus.edu.sg`, subject **Verify your
CrossFlow account**, and Message-ID
`202610230849.10374884722@smtp-relay.sendinblue.com`. It contains no OTP.
Brevo accepted and marked that exact message Delivered. Brevo's documentation
explains that this records delivery to the recipient server, which may still
filter, quarantine or later reject it; it does not certify inbox receipt.
[Brevo delivery-status explanation](https://help.brevo.com/hc/en-us/articles/17677373572626-FAQs-Why-emails-are-marked-delivered-but-are-not-received).

Earlier read-only Gmail checks found no matching message in NUS all folders/spam/trash
or by the exact Message-ID. The same ID was absent from EJC Gmail. NUS Gmail
showed no blocked sender, CrossFlow filter or forwarding address, and no
external-fetch section; no settings were changed. Provider Opened events are
not inbox evidence because a preview can trigger them.

The school's [published SoC links](https://www.comp.nus.edu.sg/~ooiwt/mysoc.html)
point to [SoC webmail](https://mysoc.nus.edu.sg/~webmail/). The current browser
attempt timed out with **This site cannot be reached**. That is a connectivity
result, not an authentication failure or evidence that Gmail is the wrong
client. Use the school's normal authorized network/VPN access if required.

The [SoC Facilities and IT Services contact route](https://www.comp.nus.edu.sg/contactus/)
and [support portal](https://rt.comp.nus.edu.sg/) were identified for a possible
recipient-server trace. That handoff is now historical and unnecessary for the
resolved message. Its recorded Message-ID/timestamp contain no OTP or password;
no credentials or code values were sent to support.
