# Setting up `dev@ampliconrepository.org` to receive mail

## Why this document exists

The Privacy page needs a contact address for privacy questions, account
corrections, and account-deletion requests. The obvious candidate was
`dev@ampliconrepository.org` — it is on the site's own domain and already appears
in the repository as the default `EMAIL_FROM` in `check_disk.py`.

It cannot receive mail today. As of August 2026:

```console
$ dig +short MX ampliconrepository.org @1.1.1.1     # no output
$ dig +short TXT ampliconrepository.org @1.1.1.1
"v=spf1 include:spf.mailjet.com ?all"
```

No MX records at all, so there is nothing to deliver inbound mail to and anything
sent to `dev@ampliconrepository.org` bounces. The only mail-related DNS is an SPF
record authorising Mailjet to *send* as the domain. The domain is send-only.

So the Privacy page currently lists `jluebeck@ucsd.edu` instead. That works, but
it is a personal address on a public page, and it ties the site's contact route to
one person. Pointing it at a role address on the site's own domain is better:
the address outlives whoever is reading it this year, and it can be redirected
without editing and redeploying the site.

## What needs to change

Two things, in this order:

1. Make `dev@ampliconrepository.org` deliver somewhere (below).
2. Change one line in `caper/templates/pages/privacy.html` — the `mailto:` in the
   "Questions, corrections, and account deletion" section — and the matching
   assertion in `tests/test_privacy_terms_pages.py`.

Do them in that order. A published address that bounces is worse than a personal
address that works.

## Where the DNS lives

Route 53. The domain's nameservers are AWS:

```console
$ dig +short NS ampliconrepository.org @1.1.1.1
ns-1375.awsdns-43.org.
ns-1706.awsdns-21.co.uk.
ns-506.awsdns-63.com.
ns-888.awsdns-47.net.
```

So every option below is a matter of adding records to the existing Route 53
hosted zone. Adding MX records to the zone apex does not disturb the A records
that serve the website — they are different record types on the same name.

## Option A — a mail forwarding service (recommended for "forward it to me")

The least machinery for exactly the stated goal. Services such as
[ImprovMX](https://improvmx.com) or [Forward Email](https://forwardemail.net)
take mail for your domain and forward it to an existing mailbox; both have a free
tier that covers a handful of aliases. There is no mailbox to maintain and no
new password to look after.

Steps:

1. Create an account with the forwarder and add `ampliconrepository.org`.
2. In the Route 53 hosted zone, add the MX records it gives you at the zone
   apex. They will look something like:

   ```text
   ampliconrepository.org.  MX  10 mx1.<provider>.com.
   ampliconrepository.org.  MX  20 mx2.<provider>.com.
   ```

3. Add whatever TXT record the provider asks for to verify the domain.
4. In the provider's dashboard, create the alias:
   `dev@ampliconrepository.org` → `jluebeck@ucsd.edu`.
5. Verify end to end by sending a message to `dev@ampliconrepository.org` from an
   address outside the domain and confirming it arrives.

Caveats worth knowing before choosing this:

- Forwarded mail arrives *from* the original sender, which can trip SPF/DMARC
  checks at the receiving end. Reputable forwarders handle this by rewriting the
  envelope sender (SRS); confirm yours does.
- Replies sent from your UCSD mailbox come *from* your UCSD address, not from
  `dev@`. For a low-volume contact address that is usually fine. If replies need
  to appear to come from `dev@`, you need a real mailbox — Option C.

## Option B — Amazon SES inbound

AWS-native, so it stays inside the account that already holds the hosted zone and
the S3 buckets, and it costs essentially nothing at this volume.

1. Confirm SES email *receiving* is available in the region you want to use —
   inbound is supported in a smaller set of regions than outbound, and the list
   changes, so check the current
   [SES receiving documentation](https://docs.aws.amazon.com/ses/latest/dg/receiving-email.html)
   rather than assuming.
2. Verify the domain in SES and publish the DKIM records it generates.
3. Add SES's inbound MX record for the chosen region at the zone apex:

   ```text
   ampliconrepository.org.  MX  10 inbound-smtp.<region>.amazonaws.com.
   ```

4. Create a receipt rule set with a rule matching `dev@ampliconrepository.org`.
   For forwarding, the usual shape is: deliver to an S3 bucket, trigger a Lambda,
   and have the Lambda re-send through SES to `jluebeck@ucsd.edu`. AWS publishes
   a reference implementation for this.

More moving parts than Option A, and the Lambda is a thing that can quietly
break, so it is only worth it if you want the mail to stay inside AWS.

## Option C — a real mailbox

If `dev@` should be somewhere you can log into and reply *from* — for example if
more than one person will handle these requests — then it needs an actual
mailbox: Google Workspace, Fastmail, Migadu, or a UCSD-provided shared mailbox if
one can be arranged. Same DNS shape as the other options (MX records at the
apex, plus DKIM), but with a per-user cost and an account to administer.

This is the right answer eventually. It is more than "forward it to me for now"
needs today.

## Do not break outbound mail

Whatever you choose, leave the existing SPF record's Mailjet include in place:

```text
v=spf1 include:spf.mailjet.com ?all
```

That record authorises the site's *outbound* notification mail (membership
changes, project updates, download links — see `EMAIL_HOST_USER` in
`caper/config.sh`). Inbound and outbound are independent; adding MX records does
not affect it, but a well-meaning cleanup of "unused" TXT records would.

Note also that the site currently sends as `ampliconrepository@cloud.ucsd.edu`,
not as an `@ampliconrepository.org` address. If `dev@` ever becomes the sending
address too, the SPF record needs to authorise whatever sends it, and
`EMAIL_HOST_USER` needs updating.

## After it works

Update, in one commit:

- `caper/templates/pages/privacy.html` — the `mailto:` link and its visible text.
- `tests/test_privacy_terms_pages.py` — `test_privacy_page_covers_the_required_topics`
  asserts the address, so it will fail until updated. That is deliberate: the
  address on that page should not change without someone noticing.
