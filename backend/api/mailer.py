"""Sends the photographer (or event planner) their upload link and upload code by email, through Amazon SES.

Turned on by two stack parameters:
  SenderEmail  an address (or an address at a domain) verified in SES in this region
  SiteUrl      the dashboard site, e.g. https://main.d123.amplifyapp.com
Links are always built from SiteUrl, never from anything the browser sends, so the system can't be used
to email arbitrary links. Each organisation can send at most DAILY_LIMIT invites a day.
"""
import html
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

SENDER = os.environ.get("SENDER_EMAIL", "").strip()
SITE_URL = os.environ.get("SITE_URL", "").strip().rstrip("/")
BRAND = "YaPhoto"
MAX_RECIPIENTS = 3
DAILY_LIMIT = 50
EMAIL_RE = re.compile(r"^[^@\s,;<>\"']{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")

ses = boto3.client("sesv2")


def configured():
    return bool(SENDER and SITE_URL)


def parse_recipients(value, err):
    """Accepts a list or a comma/space separated string. Returns up to MAX_RECIPIENTS valid, de-duplicated addresses."""
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    if not isinstance(value, list):
        raise err(400, "Send the photographer's email as text or a list.")
    out = []
    for v in value:
        v = str(v or "").strip().lower()
        if not v:
            continue
        if len(v) > 254 or not EMAIL_RE.match(v):
            raise err(400, f"{v} doesn't look like an email address.")
        if v not in out:
            out.append(v)
    if len(out) > MAX_RECIPIENTS:
        raise err(400, f"Send the link to at most {MAX_RECIPIENTS} people at a time.")
    return out


def upload_link(slug, key):
    return f"{SITE_URL}/upload.html?e={slug}#k={quote(key, safe='')}"


def _take_quota(table, org_id, n):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        table.update_item(Key={"pk": f"ORG#{org_id}", "sk": f"MAILCOUNT#{day}"},
                          UpdateExpression="ADD sent :n SET expiresAt = :exp",
                          ConditionExpression="attribute_not_exists(sent) OR sent <= :max",
                          ExpressionAttributeValues={":n": n, ":max": DAILY_LIMIT - n, ":exp": int(time.time()) + 3 * 86400})
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _dates(start, end):
    def f(d):
        x = datetime.strptime(d, "%Y-%m-%d")
        return f"{x.day} {x:%B %Y}"
    try:
        return f(start) if start == end else f"{f(start)} to {f(end)}"
    except ValueError:
        return start


def _message(event_name, sender_name, slug, key, start, end):
    link = upload_link(slug, key)
    when = _dates(start, end)
    subject = f"Your upload link for {event_name}"[:180]
    text = (
        f"Hello,\n\n{sender_name} has set you up to upload photos for {event_name} ({when}) on {BRAND}.\n\n"
        f"Your upload link (keep it private, it works like a password):\n{link}\n\n"
        f"Upload code, if you're asked for it: {key}\n\n"
        "How it works:\n"
        "1. Open the link on the laptop or phone that has the photos.\n"
        "2. Sort into Shared and Private folders if you like, or leave them for the AI.\n"
        "3. Drag the folder in. Guests find their photos within seconds of each upload.\n\n"
        f"If you weren't expecting this, you can ignore it.\n\n{BRAND}\n"
    )
    e = html.escape
    page = f"""<!doctype html><html><body style="margin:0;background:#f4f1ee;font-family:Helvetica,Arial,sans-serif;color:#1d1622">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:8px">
<tr><td style="padding:28px 28px 8px;font-family:Georgia,serif;font-size:22px">Ya<i style="color:#a8875a">Photo</i></td></tr>
<tr><td style="padding:8px 28px 0;font-size:16px;line-height:1.6">
<p style="margin:0 0 14px"><b>{e(sender_name)}</b> has set you up to upload photos for <b>{e(event_name)}</b> ({e(when)}).</p>
<p style="margin:0 0 22px"><a href="{e(link)}" style="display:inline-block;background:#1d1622;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:999px">Open your upload page</a></p>
<p style="margin:0 0 6px;color:#5e5463;font-size:14px">Upload code, if you're asked for it:</p>
<p style="margin:0 0 22px;font-family:Consolas,monospace;font-size:15px;background:#f4f1ee;padding:10px 12px;border-radius:6px;word-break:break-all">{e(key)}</p>
<p style="margin:0 0 8px;font-size:14px;color:#5e5463">Open the link on the laptop or phone that has the photos, sort them into Shared and Private folders if you like (or leave them for the AI), and drag the folder in. Guests find their photos within seconds of each upload.</p>
<p style="margin:16px 0 0;font-size:13px;color:#8a8090">Keep this link private: anyone with it can upload to the event. If you weren't expecting this email, you can ignore it.</p>
</td></tr><tr><td style="padding:24px 28px 28px;font-size:12px;color:#8a8090">Sent by {BRAND} on behalf of {e(sender_name)}</td></tr>
</table></td></tr></table></body></html>"""
    return subject, text, page


def send_upload_invites(table, org_id, recipients, event_name, sender_name, slug, key, start, end):
    """Returns [{email, sent, error?}]. Never raises for delivery problems: event creation must not fail because of email."""
    if not recipients:
        return []
    if not configured():
        return [{"email": r, "sent": False, "error": "Email isn't set up yet."} for r in recipients]
    if not _take_quota(table, org_id, len(recipients)):
        return [{"email": r, "sent": False, "error": "Daily email limit reached. Copy the link instead."} for r in recipients]
    subject, text, page = _message(event_name, sender_name, slug, key, start, end)
    results = []
    for r in recipients:
        try:
            ses.send_email(
                FromEmailAddress=f"{BRAND} <{SENDER}>", Destination={"ToAddresses": [r]},
                Content={"Simple": {"Subject": {"Data": subject, "Charset": "UTF-8"},
                                    "Body": {"Text": {"Data": text, "Charset": "UTF-8"}, "Html": {"Data": page, "Charset": "UTF-8"}}}},
            )
            results.append({"email": r, "sent": True})
        except ClientError as err:
            code = err.response["Error"]["Code"]
            print(f"upload invite to {r} failed: {code} {err}")
            msg = ("This address isn't verified in SES yet (sandbox mode)." if code == "MessageRejected"
                   else "Couldn't send. Copy the link instead.")
            results.append({"email": r, "sent": False, "error": msg})
    return results
