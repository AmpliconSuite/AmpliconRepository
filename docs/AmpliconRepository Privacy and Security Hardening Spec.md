# AmpliconRepository Privacy and Security Hardening Spec

## Purpose

Make a small, targeted set of changes to AmpliconRepository to improve privacy/security hygiene and add minimal, appropriate privacy disclosures for a public academic research repository.

The guiding principle is **minimal compliance and least-data/least-permission design**. Do not introduce a large compliance framework, consent-management platform, self-service GDPR tooling, or substantial new infrastructure.

Repository:

`https://github.com/AmpliconSuite/AmpliconRepository`

Production site:

`https://ampliconrepository.org/`

Before modifying code, inspect the current implementation and verify the assumptions below against the repository. Preserve existing behavior unless explicitly changed by this spec.

---

# Priority 0: Fix potentially public admin CSV exports

## Background

During review, two CSV download views in `caper/caper/views_admin.py` appeared to lack the staff-only protection used by neighboring admin views:

- `user_stats_download`
- `project_stats_download`

The user export appears to contain fields including:

- username
- email
- account creation date
- last login

The project export appears to expose project-level information including project-member identifiers.

These routes should never be available to anonymous or ordinary authenticated users.

## Required changes

Protect both views using the same staff-only authorization mechanism already used for the surrounding admin-statistics views.

Prefer consistency with the existing codebase rather than introducing a new permission system.

For example, if neighboring views use:

```python
@user_passes_test(lambda u: u.is_staff, login_url="/notfound/")
```

apply equivalent protection to both download views.

Also inspect all routes under the admin statistics functionality for similar omissions.

## Tests

Add tests confirming that:

1. An anonymous user cannot access either CSV endpoint.
2. A normal authenticated non-staff user cannot access either endpoint.
3. A staff user can access the endpoint.
4. The successful response remains a CSV download with the expected content type.

Do not query the production endpoint as part of this work.

---

# Priority 1: Remove Google Analytics completely

## Background

AmpliconRepository currently includes Google Analytics / GA4 sitewide in the base template.

We do not meaningfully use these analytics and do not want to maintain a cookie-consent system solely for analytics.

## Required changes

Remove Google Analytics from the site entirely.

Inspect the repository for:

- `gtag`
- `googletagmanager`
- the current GA measurement ID
- Google Analytics JavaScript
- any other GA initialization code

Remove all GA-specific code.

Do not replace Google Analytics with another analytics product.

## Acceptance criteria

After the change:

- no Google Analytics script is loaded;
- no GA measurement ID remains in active templates/settings;
- normal application functionality is unaffected.

---

# Priority 2: Add a concise Privacy page

Create a first-party privacy page on the AmpliconRepository domain, preferably:

`/privacy/`

The intent is to provide a short, plain-language notice appropriate for a UC San Diego academic research resource.

Do not generate a lengthy corporate privacy policy.

## Required content

The Privacy page should substantially communicate the following.

### Operator

AmpliconRepository is an academic research resource developed and maintained by the Bafna Lab at the University of California San Diego.

Avoid making unsupported legal claims about whether UC San Diego or the Regents is formally the legal data controller.

### Account information

Explain that users who create or use accounts may provide or associate information such as:

- username;
- email address;
- account/profile information;
- authentication information.

Do not imply that passwords from Google or Globus are received or stored by AmpliconRepository.

### Third-party authentication

Explain that users may authenticate through providers such as:

- Google;
- Globus.

State that AmpliconRepository receives limited account/profile information made available through the authentication provider, such as name/profile information and email address as applicable.

The wording should remain sufficiently general that modest OAuth implementation changes do not immediately make the policy inaccurate.

### Repository/project functionality

Explain that account-related information may be used for:

- repository accounts;
- project ownership/membership;
- project access;
- notifications;
- uploads/submissions;
- API access;
- other functionality requested by the user.

### Cookies/session storage

Do **not** create a separate Cookies Policy.

Include a short section explaining that AmpliconRepository uses cookies or similar browser/session mechanisms necessary for functionality such as:

- login/authentication;
- security;
- CSRF protection;
- maintaining sessions;
- preventing abusive automated downloads or requests.

Do not describe analytics cookies after GA has been removed.

Do not add a cookie banner unless inspection discovers non-essential tracking that requires one.

### reCAPTCHA / anti-abuse services

Mention that third-party anti-abuse or CAPTCHA services may be used for functionality such as account creation or downloads and that those providers may receive technical information necessary to provide those services.

Avoid overstating exactly what Google collects unless the implementation clearly establishes it.

### Server and application logs

Explain that operational/security logs may contain information such as:

- IP address;
- browser/user-agent information;
- requested URLs;
- timestamps;
- referrer information;
- technical/error information.

State that these logs are used for security, troubleshooting, and operation of the repository.

Use wording such as "retained for a limited period" rather than promising a specific retention duration unless it is centrally enforced and well established.

### Project audit/history data

Explain that limited historical information associated with repository submissions or projects may be retained when necessary for:

- repository integrity;
- provenance;
- audit/history;
- security;
- reproducibility.

This is important so that account deletion does not imply that every historical audit record must be erased.

### Email

Explain that AmpliconRepository may send functional/transactional emails related to:

- account activity;
- project membership;
- repository updates;
- requested downloads or processing.

Do not characterize these as marketing.

### API access

Mention that authenticated users may be issued API credentials/tokens associated with their accounts.

Do not expose implementation details that would weaken security.

### No advertising / sale of information

State plainly that AmpliconRepository:

- does not serve targeted advertising;
- does not sell user personal information.

Do not make broader promises that cannot be verified.

### Contact / account requests

Provide a site contact mechanism for users who want to:

- ask privacy questions;
- correct account information;
- request account deletion.

Reuse an appropriate existing AmpliconRepository/Bafna Lab support email if one already exists in the repository/site.

Do not invent a new email address.

### UC San Diego privacy statement

Include a link to the existing UC San Diego Website Privacy Statement as additional institutional information.

The AmpliconRepository page should remain the first-party privacy URL used for the site.

---

# Priority 3: Add a concise Terms of Use page

Create:

`/terms/`

Keep it short and suited to a free academic research resource.

## Required topics

Include language substantially covering:

### Academic resource

AmpliconRepository is provided as an academic/scientific resource.

### No warranty

The service, predictions, annotations, and other content are provided without warranties of accuracy, completeness, fitness for a particular purpose, or uninterrupted availability.

Do not over-lawyer the wording.

### Research use / interpretation

Repository predictions and annotations should not be treated as medical advice, clinical diagnosis, or a substitute for professional clinical interpretation.

### User submissions

Users are responsible for ensuring that they have the right and appropriate authorization to upload or submit data.

Do not attempt to write detailed HIPAA, IRB, genomic privacy, or controlled-access rules into these general Terms.

A concise statement is enough.

### Service operation

AmpliconRepository may modify, remove, or discontinue content or functionality as necessary for technical, scientific, security, or administrative reasons.

### Abuse

Users may not intentionally:

- interfere with the service;
- bypass security controls;
- misuse accounts or API access;
- upload unlawful or unauthorized material.

### External resources

The site may link to external tools/resources and does not control their availability or policies.

### UC San Diego terms

Include a link to UC San Diego's general website Terms of Use or other appropriate institutional terms.

Do not invent contractual terms that conflict with UC policy.

---

# Priority 4: Add footer links

Add unobtrusive sitewide footer links:

`Privacy · Terms`

Link them to:

- `/privacy/`
- `/terms/`

Match the existing footer visual style.

Do not add banners, modals, forced acknowledgement, or account-registration checkboxes unless required by existing application behavior.

---

# Priority 5: Reduce unnecessary retention of web access logs

## Background

The checked-in logrotate configuration currently appears to retain approximately 52 weekly rotations.

The Gunicorn access log records information including client IP address, request information, referrer, browser/user-agent information, and request timing.

We do not need roughly one year of these logs for routine operation.

## Required change

Change the repository-provided logrotate configuration to retain approximately three months of weekly logs.

Preferred configuration:

```text
weekly
rotate 12
```

If deployment configuration differs from the checked-in config, do not silently modify unrelated infrastructure. Document the discrepancy.

Preserve compression and existing safe logrotate behavior.

## Documentation

Update comments that currently describe one-year retention.

---

# Priority 6: Improve account-deletion cleanup

## Background

The application appears to store account-associated identifiers outside Django's core user record.

Examples may include:

- project membership lists;
- project subscriber/notification lists;
- user preference documents keyed by email;
- API credentials/tokens;
- historical project audit records.

The current deletion process should be reviewed to ensure obvious active account-associated data is cleaned up.

## Required behavior

When an administrator deletes a user account:

1. Delete the Django user account normally.
2. Revoke/delete API credentials associated with the account through the normal model relationships.
3. Remove both the user's **username and email address**, where applicable, from active:
   - project member lists;
   - subscriber lists;
   - notification lists.
4. Delete user-preference records specifically associated with that user's email/account.
5. Preserve historical project audit/provenance records when they serve a legitimate repository-history purpose.

Do not attempt to rewrite or destroy historical project provenance merely because an account was deleted.

## Tests

Add focused tests confirming cleanup of:

- username membership;
- email-address membership;
- notification/subscriber references;
- user preferences;
- API credentials if not already handled through cascading deletion.

Include a test that verifies historical audit/provenance records are not unintentionally deleted.

---

# Priority 7: Reduce unnecessary personal information in logs

Search the codebase for application logging or `print()` calls that place user email addresses or similar identifiers into stdout/application logs.

Known areas to inspect include:

- membership notifications;
- project membership changes;
- project upload/audit code.

## Required changes

Where the email/identifier is not necessary for debugging or audit purposes:

- remove it from the log;
- log a non-PII event description instead.

Example:

Instead of:

```python
print("send project add email to " + user_obj.email)
```

prefer something such as:

```python
logger.info("Sending project membership notification")
```

Use the project's existing logging conventions where possible rather than adding new ad hoc logging infrastructure.

## Important distinction

Do **not** remove identifiers from structured project audit/provenance records merely because they are personal data if those records are needed to establish repository history.

The goal is specifically to avoid duplicating personal identifiers into general application/server logs unnecessarily.

---

# Priority 8: Review and minimize Globus OAuth scopes

## Background

The current Globus OAuth configuration appears to request:

```text
openid
profile
email
urn:globus:auth:scope:transfer.api.globus.org:all
```

The repository review did not identify obvious use of Globus Transfer functionality.

## Required work

Search the complete codebase for any use of:

- Globus Transfer APIs;
- transfer clients;
- transfer tokens;
- endpoints requiring the broad `transfer.api.globus.org:all` scope.

If the Transfer scope is genuinely unused, remove it and request only the minimum authentication/profile scopes needed:

```text
openid
profile
email
```

If Transfer functionality exists and legitimately requires the scope:

- keep it;
- document where and why it is used;
- make no change simply for the sake of satisfying this spec.

Least privilege is the goal.

## Tests

Confirm Globus login still works at the application level after any scope change.

Do not require live credentials in the automated test suite.

---

# Priority 9: Restore normal CSRF protection for API-token management

## Background

Inspect the API-token management endpoint in `views_apis.py`.

It currently appears to deliberately bypass DRF/Django CSRF enforcement even though authenticated requests can create/replace or revoke an API token.

That is unnecessary risk for a state-changing authenticated operation.

## Required changes

Restore normal CSRF protection for browser/session-authenticated API-token management requests.

Adjust the frontend JavaScript/request code to send the existing Django CSRF token correctly if necessary.

Do not break legitimate API clients that authenticate using API tokens rather than browser sessions.

## Tests

Verify:

1. unauthenticated requests cannot manage a user's API token;
2. browser/session-authenticated state-changing requests require valid CSRF protection;
3. valid UI requests continue to work;
4. unrelated token-authenticated API requests remain functional.

---

# Priority 10: Review cookies and external third-party requests after GA removal

Perform a code-level review of templates and frontend dependencies for third-party resources, including:

- Google;
- Globus;
- reCAPTCHA;
- JavaScript CDNs;
- CSS/font CDNs;
- other externally loaded scripts.

The goal is **not** to self-host every library.

Instead, determine whether anything remaining performs non-essential behavioral tracking.

If only functional/security/authentication resources remain, do not add a cookie consent banner.

Document notable third-party services in the implementation summary.

---

# Out of scope

Do **not** add any of the following unless an existing requirement clearly demands it:

- OneTrust;
- Cookiebot;
- a general consent-management platform;
- a cookie preference center;
- a separate Cookie Policy;
- marketing consent;
- a GDPR self-service portal;
- automated data-download/export requests;
- a new analytics platform;
- tracking pixels;
- advertising infrastructure;
- broad HIPAA compliance functionality;
- IRB workflow;
- genomic-data consent management;
- major authentication redesign;
- major database migration;
- extensive legal boilerplate.

This should remain a small privacy/security hardening change.

---

# Deployment/configuration caveat

Repository configuration may not represent the full production environment.

In particular, inspect or flag differences involving:

- `local_settings.py`;
- environment/config shell files;
- AWS infrastructure;
- load balancer logs;
- CloudWatch;
- S3 access logs;
- production log retention;
- other UCSD-managed infrastructure outside this repository.

Do not block the code changes merely because those systems are outside the repository.

Instead, include a short note in the final implementation report identifying anything that could not be verified from source.

Avoid putting exact infrastructure retention promises into the Privacy page unless they can be reliably established.

---

# Tests and validation

At minimum, run the existing test suite relevant to the modified modules and add regression tests for security-sensitive changes.

Specifically test:

- staff-only CSV access;
- account deletion cleanup;
- API-token CSRF behavior;
- Privacy page returns HTTP 200;
- Terms page returns HTTP 200;
- footer links are present;
- GA script is absent from rendered pages.

Also run a repository-wide search after implementation for:

```text
gtag
googletagmanager
G-RLJSFEY3H0
Google Analytics
transfer.api.globus.org:all
```

Report any remaining matches and whether they are intentional.

---

# Deliverables

Produce one focused PR containing the changes above.

In the PR summary, provide:

## Security fixes

List security/privacy issues fixed, prominently including the admin CSV authorization issue.

## Data minimization

List:

- GA removal;
- reduced log retention;
- PII logging cleanup;
- any Globus scope reduction.

## Privacy/Terms

Summarize the new pages and footer links.

## Account deletion

Describe what account-associated records are now cleaned up and what historical records are intentionally retained.

## Production items requiring manual verification

List any privacy-relevant configuration that cannot be determined from the repository itself.

## Tests

List tests added and commands run.

---

# Implementation philosophy

Prefer deleting unnecessary collection and permissions over documenting them.

Examples:

- If analytics is unused, remove analytics rather than build analytics consent.
- If an OAuth scope is unused, remove the scope rather than explain why it exists.
- If personal information does not need to be logged, stop logging it rather than inventing a retention policy for it.
- If ordinary framework security protections already exist, use them rather than creating custom mechanisms.

The desired end state is a boring, low-data academic website with:

- necessary account/session functionality;
- appropriately protected administrative data;
- minimal third-party tracking;
- short Privacy and Terms pages;
- no unnecessary compliance machinery.