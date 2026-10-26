"""External wake-up, e.g. scheduled GitHub Actions. No secrets in URLs/logs."""
import os
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse


def main():
    url=os.environ.get('PLANNER_URL','').rstrip('/')
    secret=os.environ.get('CRON_SECRET','')
    if not url or not secret:
        raise SystemExit('Set PLANNER_URL and CRON_SECRET in scheduler secrets first.')
    if urlparse(url).scheme!='https':
        raise SystemExit('PLANNER_URL must use HTTPS')
    for attempt in range(3):
        try:
            request=urllib.request.Request(url+'/cron/tick',data=b'',headers={'Authorization':'Bearer '+secret},method='POST')
            with urllib.request.urlopen(request,timeout=100) as response:
                if response.status==200:
                    print('Scheduler tick accepted.'); return
        except urllib.error.HTTPError as exc:
            if exc.code in (401,403): raise SystemExit('Scheduler authentication failed.') from None
        except (urllib.error.URLError,TimeoutError):
            pass
        if attempt<2: time.sleep(10)
    raise SystemExit('Scheduler endpoint unavailable after retries.')


if __name__=='__main__': main()
