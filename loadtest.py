"""Simple load test for the AMS or the Helpdesk: many signed-in users opening pages at once.

    python loadtest.py http://127.0.0.1:5000 --users 50 --seconds 60 --login admin:PASSWORD

Each simulated user signs in once, then opens the pages listed below, pausing --think seconds between pages
(real people read a page before clicking; --think 0 hammers the server as hard as it can).
It reports requests per second, how slow the slowest pages were, and any errors.
Run it against a test copy on the real server, never against the live system during work hours.
Standard library only.
"""
import argparse
import http.cookiejar
import re
import statistics
import threading
import time
import urllib.parse
import urllib.request

PAGES = {"ams": ["/", "/assets", "/assets?page=2", "/issues", "/reports", "/assets/1"],
         "helpdesk": ["/", "/tickets", "/tickets?scope=all", "/tickets/1", "/analytics"]}


def session(base, user, password):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    page = op.open(base + "/login", timeout=30).read().decode()
    token = re.search(r'name="csrf" value="(\w+)"', page).group(1)
    op.open(base + "/login", urllib.parse.urlencode({"username": user, "password": password, "csrf": token}).encode(), timeout=30)
    return op


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--login", required=True, help="username:password of a test account")
    ap.add_argument("--think", type=float, default=1.0, help="seconds each user waits between pages (default 1)")
    ap.add_argument("--app", choices=PAGES, help="which page list (guessed from the login page if left out)")
    a = ap.parse_args()
    base, (user, password) = a.base.rstrip("/"), a.login.split(":", 1)
    app = a.app or ("helpdesk" if b"Helpdesk" in urllib.request.urlopen(base + "/login", timeout=30).read() else "ams")
    times, errors, lock = [], [], threading.Lock()
    stop = time.time() + a.seconds

    def worker(n):
        try:
            op = session(base, user, password)
        except Exception as e:
            with lock:
                errors.append(f"sign-in: {e}")
            return
        i = n
        while time.time() < stop:
            url = base + PAGES[app][i % len(PAGES[app])]
            i += 1
            t0 = time.perf_counter()
            try:
                op.open(url, timeout=60).read()
                with lock:
                    times.append(time.perf_counter() - t0)
            except Exception as e:
                with lock:
                    errors.append(f"{url}: {e}")
            time.sleep(a.think)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(a.users)]
    started = time.time()
    [t.start() for t in threads]
    [t.join() for t in threads]
    took = time.time() - started
    if not times:
        print("No page loaded.", *errors[:5], sep="\n")
        return
    ms = sorted(t * 1000 for t in times)
    print(f"{app}: {a.users} users for {a.seconds}s, {a.think}s between pages")
    print(f"  pages loaded   {len(ms)}  ({len(ms) / took:.1f} per second)")
    print(f"  typical page   {statistics.median(ms):.0f} ms")
    print(f"  slowest 5%     {ms[int(len(ms) * 0.95)]:.0f} ms and above")
    print(f"  slowest        {ms[-1]:.0f} ms")
    print(f"  errors         {len(errors)}" + ("".join(f"\n    {e}" for e in errors[:5])))


if __name__ == "__main__":
    main()
