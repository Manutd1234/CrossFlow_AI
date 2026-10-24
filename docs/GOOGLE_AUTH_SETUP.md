# Google sign-in for the local app

Email/password sign-in uses a **CrossFlow account password** managed by
Supabase. It does not use a Gmail mailbox password. “Continue with Google”
opens Google's own sign-in screen instead.

The app code is local. Google OAuth credentials must be configured in the
hosted Supabase project before Google can issue a real session.

## Current setup status

The Codex browser is signed in to Google Cloud. **CrossFlow AI**
(`crossflow-ai-512506`) was created under `ianwangsa2-org` with the explicitly
approved **My Billing Account** selection. No paid account upgrade was chosen.

The OAuth application was **saved** after the account owner's explicit
acceptance of Google's API Services User Data Policy. It uses app name
**CrossFlow AI**, `ianwangsa2@gmail.com` as support/contact email, and an external
audience in testing mode.

The **CrossFlow AI Local Web** client was **created** with origin
`http://localhost:3000` and redirect URI
`https://wtednggryrikyvkuhqjo.supabase.co/auth/v1/callback`. The three identity
scopes (`openid`, `userinfo.email`, `userinfo.profile`) were saved and verified
after a fresh page load. Supabase now has the correct Client ID and a nonempty
masked saved secret, and the provider list shows **Google Enabled**.
**A fresh Google OAuth round trip from `http://localhost:3000` succeeded.**
The app showed **Ian Wangsa** and the backend resolved the protected profile's
approved **admin** role. See [fresh login evidence](verification/google-login-admin-success.png).
No generated secret was written into source files or verification screenshots.
Secret replacement and old-secret revocation remain unverified; this successful
login does not prove that rotation occurred.

The app remains in external **Testing** mode. The signed-in owner
`ianwangsa2@gmail.com` was added as a test user after explicit authorization;
the saved audience grid was verified. Google's
[basic identity scope exception](https://support.google.com/cloud/answer/15549945?hl=en)
means `openid`, email and profile alone do not require test-user allowlisting.
The generic console testing banner should not be treated as a complete rule
for this scope set. The fresh local Google sign-in is verified as described above.

The hosted auth schema was upgraded with the minimal
`backend/auth/migrations/002_harden_auth_role_guard.sql` patch. A fresh read-only
query confirmed version **2**, RLS enabled, the role guard using **SECURITY
INVOKER**, and all **3** existing profiles retained. This patch changes only
functions; it does not promote users or backfill profiles. PostgreSQL tests
cover role protection, preserved permissions, reruns and complete rollback.

## Configure Google

1. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview)
   in your Google Cloud project. Complete the app branding/audience setup.
   Basic identity scopes use the exception above; test-user allowlisting is
   not a prerequisite for this sign-in scope set.
2. In [Clients](https://console.cloud.google.com/auth/clients), create an OAuth
   client of type **Web application**. Add `http://localhost:3000` as an
   authorized JavaScript origin.
3. Add this exact authorized redirect URI in Google:
   `https://wtednggryrikyvkuhqjo.supabase.co/auth/v1/callback`.
4. Open [Supabase's Google provider](https://supabase.com/dashboard/project/wtednggryrikyvkuhqjo/auth/providers?provider=Google).
   Enter the Google Client ID and Client Secret there, enable Google, and save.
   Keep the secret out of frontend environment files and source control.
5. [Supabase URL Configuration](https://supabase.com/dashboard/project/wtednggryrikyvkuhqjo/auth/url-configuration)
   already has `http://localhost:3000` as Site URL. Use
   **http://localhost:3000/** for the local login test. Other origins, such as
   `http://127.0.0.1:3000/`, need their own redirect allowlist entry. Start and
   finish the login in the same browser tab and origin.
6. The tested minimal v1→v2 authentication upgrade is already applied to this
   hosted project. For a new project, initialize `backend/auth/schema.sql`.
   New email and Google accounts always start as drivers.

Then choose **Continue with Google** in the local app. After consent, Supabase
returns a short-lived code; the browser exchanges it using PKCE and the backend
resolves the account's role. A new Google account starts as a driver.

The Google client secret belongs only in Supabase's provider configuration.
The local app needs only the existing Supabase URL and publishable key.

Signup codes also need a configured email provider and the Confirm signup
template. See [signup email setup](SUPABASE_SIGNUP_EMAIL_SETUP.md).

See [Supabase's official Google setup guide](https://supabase.com/docs/guides/auth/social-sign-in/auth-google)
and [PKCE flow documentation](https://supabase.com/docs/guides/auth/sessions/pkce-flow).

## Credential replacement handoff

During UI inspection, the original Google client secret appeared once in a
copy-button accessibility label in tool output. **Replacement and old-secret
revocation are unverified.** The currently saved configuration supports the
successful fresh login recorded above, but its masked field does not establish
which secret is stored. The owner must complete and verify rotation, then
remove the exposed old secret after the replacement round trip succeeds.
No secret value is saved here.

Supabase's saved settings enable Google, keep nonce checks enabled and disallow
users without email. Keep those last two settings unchanged during rotation.
`ianwangsa2@gmail.com` has the explicitly approved admin/operator role, now
resolved by the backend during a fresh Google login. This verifies that
operator login. EJC's first email-code UI login succeeded as driver, survived
reload and succeeded again for the confirmed account, with zero console
errors. The separate NUS signup's 16:49 message was found in Outlook with its
header/body addressed to `ian1234@comp.nus.edu.sg`; signup OTP verification
succeeded as CrossFlow Driver Demo / driver. The CrossFlow email is unchanged
and no accounts were merged. NUS reload restoration also passed with DRIVER
from `crossflow_profiles`. A separate localhost password sign-in using the
owner-entered password also passed, followed by reload restoration and the
server-resolved DRIVER modal, with zero console errors.
[NUS password-login evidence](verification/nus-password-login-restored.png).
These checks do not
establish secret rotation or production readiness.
