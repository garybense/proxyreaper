#!/usr/bin/env python3
"""
proxyreaper — fast multi-protocol proxy harvester + OpenBullet2 syncer.

Examples:
  # Free SOCKS5 → local TCP+HTTP filter → OB2 socks5 group → check → purge dead
  python3 proxyreaper.py --type socks5 --OB2

  # Residential file (skip local HTTP, still optional TCP) → OB2
  python3 proxyreaper.py --type socks5 --OB2 --file resi.txt --skip-local --clear

  # Stricter check URL (any site you care about) + revalidate whole group
  python3 proxyreaper.py --type socks4 --OB2 --recheck-all \\\\
      --check-url https://example.com/ --success-key "Example Domain"

  # Max speed free harvest
  python3 proxyreaper.py --type socks5 --OB2 --workers 80 --bots 50 --target-count 100

  # Free mode with group already full of "working" labels: force recheck (don't just re-export)
  python3 proxyreaper.py --type socks4 --OB2 --recheck-all

Supported --file formats:
  host:port | host:port:user:pass | user:pass@host:port
  socks5://... | (Socks5)host:port:user:pass

Run with --help for full usage.
"""

from __future__ import annotations

import argparse
import pyfiglet
import random
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── constants ─────────────────────────────────────────────────────

OB2_TYPE = {
    "http": "http",
    "https": "https",
    "socks4": "socks4",
    "socks5": "socks5",
    "socks4a": "socks4a",
}

DEFAULT_GROUP_FOR_TYPE = {
    "http": "Default",
    "socks4": "socks4",
    "socks5": "socks5",
}

# Shared HTTP session (connection pool)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; proxyreaper)"})
_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=1)
SESSION.mount("http://", _ADAPTER)
SESSION.mount("https://", _ADAPTER)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


# ── ANSI Banner ────────────────────────────────────────────────────

_ANSI_BANNER = r"""
█▀█ █▀▄ ▄▀▄ █░█ ▀▄▀ █▀█ ██▀ ▄▀▄ █▀▄ ██▀ █▀▄
█▀▀ █▀▄ ▀▄▀ ▄▀▄ ░█░ █▀▄ █▄▄ █▀█ █▀░ █▄▄ █▀▄
"""

_ANSI_COLORS = [
    "\033[38;5;196m",  # red
    "\033[38;5;226m",  # yellow
    "\033[38;5;46m",   # green
    "\033[38;5;51m",   # cyan
    "\033[38;5;201m",  # magenta
    "\033[38;5;27m",   # blue
]

def render_banner() -> str:
    lines = _ANSI_BANNER.strip().split("\n")
    out = []
    for i, line in enumerate(lines):
        if line.strip():
            color = _ANSI_COLORS[i % len(_ANSI_COLORS)]
            out.append(f"\033[1m{color}{line}\033[0m")
        else:
            out.append(line)
    footer = (
        f"\033[1m\033[38;5;51mFast multi-protocol proxy harvester + OB2 syncer\033[0m\n"
        f"\033[1m\033[38;5;46mUsage: python3 proxyreaper.py --help\033[0m"
    )
    return "\n".join(out) + "\n" + footer


def show_banner():
    print(render_banner())


# ── OpenBullet2 client ────────────────────────────────────────────

class OpenBullet2Client:
    def __init__(
        self,
        base_url: str = "http://localhost:8501",
        api_key: str = "i2pphlburfxvc1ls5di97ahajfnd8ytw",
        group_name: str = "socks5",
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
        self.group_name = group_name
        self.group_id: Optional[int] = None

    def _request(self, method: str, endpoint: str, retries: int = 3, **kwargs) -> Any:
        url = f"{self.base_url}{endpoint}"
        last_err = None
        for attempt in range(retries):
            try:
                r = SESSION.request(
                    method, url, headers=self.headers, timeout=kwargs.pop("timeout", 60), **kwargs
                )
                r.raise_for_status()
                if not r.text:
                    return {}
                try:
                    return r.json()
                except Exception:
                    return {"raw": r.text}
            except Exception as e:
                last_err = e
                body = ""
                if hasattr(e, "response") and e.response is not None:
                    try:
                        body = e.response.text[:200]
                    except Exception:
                        pass
                if attempt + 1 < retries:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                log(f"OB2 API Error ({method} {endpoint}): {e} {body}")
        return None

    def get_group_id(self) -> Optional[int]:
        groups = self._request("GET", "/api/v1/proxy-group/all")
        if groups:
            for g in groups:
                if g.get("name") == self.group_name:
                    self.group_id = g["id"]
                    log(f"Using OB2 group '{self.group_name}' (id={self.group_id})")
                    return self.group_id
        log(f"Creating OB2 group '{self.group_name}'...")
        new = self._request("POST", "/api/v1/proxy-group", json={"name": self.group_name})
        if new and "id" in new:
            self.group_id = new["id"]
            log(f"Created group id={self.group_id}")
            return self.group_id
        return None

    def clear_proxies(self) -> bool:
        if self.group_id is None and not self.get_group_id():
            return False
        self._request("DELETE", "/api/v1/proxy/many", params={"ProxyGroupId": self.group_id})
        log(f"Cleared all proxies in '{self.group_name}'")
        return True

    def purge_not_working(self) -> int:
        """Delete notWorking (and optionally leave untested). Returns affected count."""
        if self.group_id is None and not self.get_group_id():
            return 0
        r = self._request(
            "DELETE",
            "/api/v1/proxy/many",
            params={"ProxyGroupId": self.group_id, "Status": "notWorking"},
        )
        n = 0
        if isinstance(r, dict):
            n = r.get("count") or r.get("affected") or r.get("entriesAffected") or 0
        log(f"Purged notWorking from '{self.group_name}' (api={r})")
        return int(n) if n else 0

    def purge_slow(self, max_ping: int = 8000) -> Any:
        if self.group_id is None and not self.get_group_id():
            return None
        r = self._request(
            "DELETE",
            "/api/v1/proxy/slow",
            params={"proxyGroupId": self.group_id, "maxPing": max_ping},
        )
        log(f"Purged slow proxies (maxPing={max_ping}): {r}")
        return r

    def existing_keys(self, proxy_type: Optional[str] = None) -> Set[str]:
        """host:port set already in group (any status)."""
        keys: Set[str] = set()
        if self.group_id is None and not self.get_group_id():
            return keys
        page = 0
        while page < 500:
            params: Dict[str, Any] = {
                "ProxyGroupId": self.group_id,
                "PageNumber": page,
                "PageSize": 200,
            }
            if proxy_type:
                params["Type"] = OB2_TYPE.get(proxy_type, proxy_type)
            data = self._request("GET", "/api/v1/proxy/all", params=params)
            if not data:
                break
            items = data.get("items") if isinstance(data, dict) else data
            if not items:
                break
            for it in items:
                host = it.get("host") or ""
                port = it.get("port")
                if host and port is not None:
                    keys.add(f"{host}:{port}")
            total_pages = data.get("totalPages") if isinstance(data, dict) else None
            if total_pages is not None:
                if page + 1 >= total_pages:
                    break
            elif len(items) < 200:
                break
            page += 1
        return keys

    def import_proxies(self, lines: List[str], proxy_type: str = "socks5") -> int:
        if self.group_id is None and not self.get_group_id():
            return 0
        ob_type = OB2_TYPE.get(proxy_type.lower(), proxy_type.lower())
        clean = [ln.strip() for ln in lines if ln and ln.strip()]
        if not clean:
            return 0
        imported = 0
        chunk_size = 1000
        for i in range(0, len(clean), chunk_size):
            chunk = clean[i : i + chunk_size]
            payload = {
                "proxyGroupId": self.group_id,
                "defaultType": ob_type,
                "proxies": chunk,
            }
            result = self._request("POST", "/api/v1/proxy/add", json=payload)
            if result is None:
                log(f"Import failed on chunk {i // chunk_size + 1}")
                break
            # AffectedEntriesDto often { count: N }
            n = 0
            if isinstance(result, dict):
                n = result.get("count") or result.get("entriesAffected") or len(chunk)
            else:
                n = len(chunk)
            imported += int(n) if n else len(chunk)
        log(f"Imported ~{imported} {ob_type} → group '{self.group_name}' (id={self.group_id})")
        return imported

    def count_by_status(self, proxy_type: Optional[str] = None) -> Dict[str, int]:
        """Fast counts using Status filter."""
        out = {"working": 0, "notWorking": 0, "untested": 0, "total": 0}
        if self.group_id is None and not self.get_group_id():
            return out
        for status in ("working", "notWorking", "untested"):
            params: Dict[str, Any] = {
                "ProxyGroupId": self.group_id,
                "Status": status,
                "PageNumber": 0,
                "PageSize": 1,
            }
            if proxy_type:
                params["Type"] = OB2_TYPE.get(proxy_type, proxy_type)
            data = self._request("GET", "/api/v1/proxy/all", params=params)
            if isinstance(data, dict) and "totalCount" in data:
                out[status] = int(data["totalCount"] or 0)
            elif isinstance(data, dict) and data.get("items") is not None:
                # fallback: no totalCount — page through lightly
                out[status] = self._count_pages(proxy_type, status)
        out["total"] = out["working"] + out["notWorking"] + out["untested"]
        return out

    def _count_pages(self, proxy_type: Optional[str], status: str) -> int:
        n = 0
        page = 0
        while page < 500:
            params: Dict[str, Any] = {
                "ProxyGroupId": self.group_id,
                "Status": status,
                "PageNumber": page,
                "PageSize": 200,
            }
            if proxy_type:
                params["Type"] = OB2_TYPE.get(proxy_type, proxy_type)
            data = self._request("GET", "/api/v1/proxy/all", params=params)
            if not data:
                break
            items = data.get("items") or []
            n += len(items)
            tp = data.get("totalPages")
            if tp is not None and page + 1 >= tp:
                break
            if len(items) < 200:
                break
            page += 1
        return n

    def run_check_job(
        self,
        bots: int = 40,
        target_url: str = "http://example.com",
        success_key: str = "Example Domain",
        timeout_ms: int = 8000,
        check_only_untested: bool = True,
    ) -> Optional[int]:
        if self.group_id is None and not self.get_group_id():
            return None
        payload = {
            "name": f"API_Check_{int(time.time())}",
            "startCondition": {
                "_polyTypeName": "relativeTimeStartCondition",
                "startAfter": "00:00:00",
            },
            "bots": bots,
            "groupId": self.group_id,
            "checkOnlyUntested": check_only_untested,
            "target": {"url": target_url, "successKey": success_key},
            "timeoutMilliseconds": timeout_ms,
            "useProxyJudge": True,
            "checkOutput": {"_polyTypeName": "databaseProxyCheckOutput"},
        }
        job = self._request("POST", "/api/v1/job/proxy-check", json=payload)
        if not job or "id" not in job:
            log(f"FAILED to create proxy-check job: {job!r}")
            return None
        job_id = job["id"]
        start = self._request("POST", "/api/v1/job/start", json={"jobId": job_id})
        # start often returns empty 200 body
        log(
            f"Started proxy-check job #{job_id} "
            f"(bots={bots}, onlyUntested={check_only_untested}, start={start!r})"
        )
        return job_id

    def delete_job(self, job_id: int) -> None:
        self._request("DELETE", "/api/v1/job", params={"id": job_id})

    def wait_for_job(
        self,
        job_id: int,
        poll: float = 1.5,
        max_wait_sec: float = 0,
        delete_when_done: bool = True,
    ) -> Dict[str, Any]:
        """Poll proxy-check job until idle/completed or max_wait_sec (0=forever)."""
        log(f"Monitoring job #{job_id}...")
        last: Dict[str, Any] = {}
        t0 = time.time()
        stalled = 0
        last_tested = -1
        try:
            while True:
                status = self._request("GET", "/api/v1/job/proxy-check", params={"id": job_id})
                if not status:
                    log("Job status request failed — aborting wait")
                    break
                last = status
                st = (status.get("status") or "").lower()
                outcome = (status.get("lastRunOutcome") or "").lower()
                raw_prog = status.get("progress")
                # API may report 0..1 fraction or 0..100 percent
                try:
                    prog_f = float(raw_prog or 0)
                except (TypeError, ValueError):
                    prog_f = 0.0
                prog = prog_f * 100.0 if prog_f <= 1.0 else prog_f
                tested = int(status.get("tested") or 0)
                total = int(status.get("total") or 0)
                print(
                    f"\r[{ts()}] #{job_id} {st.upper():10} "
                    f"{prog:5.1f}%  tested={tested}/{total}  "
                    f"OK={status.get('working', 0)}  dead={status.get('notWorking', 0)}  "
                    f"cpm={status.get('cpm', 0)}",
                    end="",
                    flush=True,
                )
                # Completion signals used by OB2 web client
                if st in ("idle", "stopped") or outcome in ("completed", "stopped", "aborted"):
                    # idle right after create is not done — only exit if we made progress or outcome set
                    if outcome in ("completed", "stopped", "aborted") or (total > 0 and tested >= total):
                        print()
                        log(f"Job #{job_id} finished status={st} outcome={outcome} tested={tested}/{total}")
                        break
                    if st == "stopped":
                        print()
                        log(f"Job #{job_id} stopped")
                        break

                if tested == last_tested and st == "running":
                    stalled += 1
                else:
                    stalled = 0
                    last_tested = tested
                # 3+ minutes with zero progress while running → give up
                if stalled > int(180 / max(poll, 0.5)) and tested == 0:
                    print()
                    log(f"Job #{job_id} stalled with tested=0 — stopping")
                    break

                if max_wait_sec and (time.time() - t0) >= max_wait_sec:
                    print()
                    log(
                        f"Job #{job_id} hit max-wait {max_wait_sec:.0f}s "
                        f"(tested={tested}/{total}) — stopping wait (job left running unless cleanup)"
                    )
                    break
                time.sleep(poll)
        finally:
            print()
            if delete_when_done:
                log(f"Stopping/deleting job #{job_id}")
                self._request("POST", "/api/v1/job/stop", json={"jobId": job_id})
                time.sleep(0.5)
                self.delete_job(job_id)
        return last

    def download_working(self, proxy_type: Optional[str] = None) -> List[str]:
        """Download working proxies from group as host:port[:user:pass] lines."""
        if self.group_id is None and not self.get_group_id():
            return []
        # Prefer download endpoint if it returns text
        params: Dict[str, Any] = {
            "ProxyGroupId": self.group_id,
            "Status": "working",
        }
        if proxy_type:
            params["Type"] = OB2_TYPE.get(proxy_type, proxy_type)
        # try download/many
        url = f"{self.base_url}/api/v1/proxy/download/many"
        try:
            r = SESSION.get(url, headers=self.headers, params=params, timeout=60)
            if r.ok and r.text and not r.text.strip().startswith("{"):
                lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
                if lines:
                    return lines
        except Exception:
            pass
        # fallback page through JSON
        lines = []
        page = 0
        while page < 500:
            p = dict(params)
            p.update({"PageNumber": page, "PageSize": 200})
            data = self._request("GET", "/api/v1/proxy/all", params=p)
            if not data:
                break
            items = data.get("items") or []
            for it in items:
                host, port = it.get("host"), it.get("port")
                user, pw = it.get("username") or "", it.get("password") or ""
                if not host or port is None:
                    continue
                if user:
                    lines.append(f"{host}:{port}:{user}:{pw}")
                else:
                    lines.append(f"{host}:{port}")
            tp = data.get("totalPages")
            if tp is not None and page + 1 >= tp:
                break
            if len(items) < 200:
                break
            page += 1
        return lines


# ── parsing ───────────────────────────────────────────────────────

def parse_proxy_line(line: str, default_type: str = "socks5") -> Optional[Dict[str, str]]:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None

    ptype = default_type.lower()
    user = password = ""

    m = re.match(r"^\(([^)]+)\)\s*(.+)$", raw, re.I)
    if m:
        t = m.group(1).lower()
        if "socks5" in t:
            ptype = "socks5"
        elif "socks4" in t:
            ptype = "socks4"
        elif "http" in t:
            ptype = "http"
        raw = m.group(2).strip()

    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()
        if "socks5" in scheme:
            ptype = "socks5"
        elif "socks4" in scheme:
            ptype = "socks4"
        elif scheme in ("http", "https"):
            ptype = "http"
        raw = rest

    if "@" in raw:
        creds, hostport = raw.rsplit("@", 1)
        if ":" in creds:
            user, password = creds.split(":", 1)
        raw = hostport

    parts = raw.split(":")
    if len(parts) == 2:
        host, port = parts[0], parts[1]
    elif len(parts) == 4:
        if parts[1].isdigit():
            host, port, user, password = parts[0], parts[1], parts[2], parts[3]
        elif parts[3].isdigit():
            user, password, host, port = parts[0], parts[1], parts[2], parts[3]
        else:
            return None
    elif len(parts) == 3 and parts[1].isdigit():
        host, port, user = parts[0], parts[1], parts[2]
        password = ""
    else:
        return None

    host, port = host.strip(), port.strip()
    if not host or not port.isdigit():
        return None
    port_i = int(port)
    if port_i < 1 or port_i > 65535:
        return None

    return {
        "type": ptype,
        "host": host,
        "port": port,
        "user": user,
        "pass": password,
        "addr": f"{host}:{port}",
        "key": f"{host}:{port}",
    }


def format_for_ob2(p: Dict[str, str]) -> str:
    if p.get("user"):
        return f"{p['host']}:{p['port']}:{p['user']}:{p['pass']}"
    return f"{p['host']}:{p['port']}"


def format_for_requests(p: Dict[str, str]) -> str:
    scheme = p["type"]
    if p.get("user"):
        u, pw = quote(p["user"], safe=""), quote(p["pass"], safe="")
        return f"{scheme}://{u}:{pw}@{p['host']}:{p['port']}"
    return f"{scheme}://{p['host']}:{p['port']}"


def load_proxies_from_file(path: str, default_type: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: Set[str] = set()
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        p = parse_proxy_line(line, default_type=default_type)
        if not p:
            continue
        # dedupe by host:port:user
        dk = f"{p['key']}:{p.get('user','')}"
        if dk in seen:
            continue
        seen.add(dk)
        out.append(p)
    log(f"Loaded {len(out)} unique proxies from {path}")
    return out


# ── free-list sources (parallel) ──────────────────────────────────

SOCKS5_SOURCES = [
    ("TheSpeedX", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt"),
    ("r00tee", "https://raw.githubusercontent.com/r00tee/Proxy-List/main/Socks5.txt"),
    ("MuRongPIG", "https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt"),
    ("hookzof", "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt"),
    ("mmpx12", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt"),
    ("ProxyScrape", "https://api.proxyscrape.com/v4/free-proxy-list/get?request=getproxies&proxy_type=socks5&timeout=10000&country=all"),
    ("Geonode", "https://proxylist.geonode.com/api/proxy-list?protocols=socks5&limit=500&page=1&sort_by=lastChecked&sort_type=desc"),
]


def _fetch_url_lines(url: str, timeout: int = 12) -> str:
    r = SESSION.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def _parse_geonode_json(text: str) -> List[str]:
    try:
        data = __import__("json").loads(text)
        rows = data.get("data") or data.get("proxies") or []
        lines = []
        for row in rows:
            ip, port = row.get("ip"), row.get("port")
            if ip and port:
                lines.append(f"{ip}:{port}")
        return lines
    except Exception:
        return []


def fetch_socks5_parallel(limit: int = 200) -> List[Dict[str, str]]:
    """Fetch all free SOCKS5 sources concurrently, merge, shuffle, return batch."""
    log(f"Fetching free SOCKS5 from {len(SOCKS5_SOURCES)} sources in parallel...")
    unique: Dict[str, Dict[str, str]] = {}

    def one(name_url: Tuple[str, str]) -> Tuple[str, int]:
        name, url = name_url
        try:
            text = _fetch_url_lines(url)
            if "geonode" in url:
                lines = _parse_geonode_json(text)
            else:
                lines = text.splitlines()
            n = 0
            for line in lines:
                p = parse_proxy_line(line, default_type="socks5")
                if p and p["type"] == "socks5":
                    unique[p["key"]] = p
                    n += 1
            return name, n
        except Exception as e:
            log(f"  {name}: FAIL {e}")
            return name, 0

    with ThreadPoolExecutor(max_workers=len(SOCKS5_SOURCES)) as ex:
        futs = [ex.submit(one, s) for s in SOCKS5_SOURCES]
        for f in as_completed(futs):
            name, n = f.result()
            if n:
                log(f"  {name}: +{n} lines")

    pool = list(unique.values())
    random.shuffle(pool)
    batch = pool[:limit]
    log(f"Free SOCKS5 pool: {len(pool)} unique → batch {len(batch)}")
    return batch


SOCKS4_SOURCES = [
    ("TheSpeedX", "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt"),
    ("mmpx12", "https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt"),
    ("ProxyScrape", "https://api.proxyscrape.com/v4/free-proxy-list/get?request=getproxies&proxy_type=socks4&timeout=10000&country=all"),
    ("Geonode", "https://proxylist.geonode.com/api/proxy-list?protocols=socks4&limit=500&page=1&sort_by=lastChecked&sort_type=desc"),
    ("socks-proxy.net", "https://www.socks-proxy.net/"),
]


def fetch_socks4_from_web(limit: int = 200) -> List[Dict[str, str]]:
    """Fetch free SOCKS4 from multiple sources (socks-proxy.net alone is unreliable)."""
    log(f"Fetching free SOCKS4 from {len(SOCKS4_SOURCES)} sources...")
    unique: Dict[str, Dict[str, str]] = {}

    def one(name_url: Tuple[str, str]) -> Tuple[str, int]:
        name, url = name_url
        try:
            text = _fetch_url_lines(url, timeout=15)
            n = 0
            if "geonode" in url:
                lines = _parse_geonode_json(text)
            elif "socks-proxy.net" in url:
                pattern = (
                    r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})</td>\s*"
                    r"<td[^>]*>(\d{2,5})</td>\s*"
                    r"<td[^>]*>[^<]*</td>\s*"
                    r"<td[^>]*>[^<]*</td>\s*"
                    r"<td[^>]*>(Socks4|Socks5)</td>"
                )
                lines = []
                for m in re.findall(pattern, text, re.I):
                    if "4" in m[2] and "5" not in m[2].lower().replace("socks", ""):
                        lines.append(f"{m[0]}:{m[1]}")
                    elif m[2].lower() == "socks4":
                        lines.append(f"{m[0]}:{m[1]}")
            else:
                lines = text.splitlines()
            for line in lines:
                p = parse_proxy_line(line, default_type="socks4")
                if p and p["type"] == "socks4":
                    unique[p["key"]] = p
                    n += 1
            return name, n
        except Exception as e:
            log(f"  {name}: FAIL {e}")
            return name, 0

    with ThreadPoolExecutor(max_workers=len(SOCKS4_SOURCES)) as ex:
        futs = [ex.submit(one, s) for s in SOCKS4_SOURCES]
        for f in as_completed(futs):
            name, n = f.result()
            if n:
                log(f"  {name}: +{n} lines")

    pool = list(unique.values())
    random.shuffle(pool)
    batch = pool[:limit]
    log(f"Free SOCKS4 pool: {len(pool)} unique → batch {len(batch)}")
    return batch


def fetch_http_table(url: str, name: str, limit: int = 50) -> List[Dict[str, str]]:
    try:
        text = _fetch_url_lines(url)
        matches = re.findall(
            r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})</td>\s*<td[^>]*>(\d{2,5})", text
        )
        out = []
        for ip, port in matches[:limit]:
            p = parse_proxy_line(f"{ip}:{port}", default_type="http")
            if p:
                out.append(p)
        log(f"{name}: {len(out)}")
        return out
    except Exception as e:
        log(f"{name} error: {e}")
        return []


# ── local testing (TCP prefilter → HTTP) ──────────────────────────

def tcp_alive(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def test_tcp_batch(
    proxies: List[Dict[str, str]], workers: int = 100, timeout: float = 2.0
) -> List[Dict[str, str]]:
    """Fast port-open filter. Drops most dead free proxies in seconds."""
    if not proxies:
        return []
    log(f"TCP prefilter: {len(proxies)} proxies ({workers} workers, {timeout}s)...")
    alive = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(tcp_alive, p["host"], int(p["port"]), timeout): p for p in proxies
        }
        done = 0
        for f in as_completed(futs):
            done += 1
            p = futs[f]
            try:
                if f.result():
                    alive.append(p)
            except Exception:
                pass
            if done % 200 == 0 or done == len(proxies):
                print(
                    f"\r[{ts()}] TCP {done}/{len(proxies)}  open={len(alive)}",
                    end="",
                    flush=True,
                )
    print()
    log(f"TCP open: {len(alive)}/{len(proxies)} ({100*len(alive)/max(1,len(alive)):.0f}%)")
    return alive


def test_http_proxy(
    proxy: Dict[str, str],
    target_url: str,
    success_key: str,
    timeout: float,
) -> Dict[str, Any]:
    proxy_url = format_for_requests(proxy)
    try:
        t0 = time.time()
        r = SESSION.get(
            target_url,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
            allow_redirects=True,
            verify=False,
        )
        elapsed = time.time() - t0
        ok = r.status_code == 200 and (not success_key or success_key in r.text)
        return {
            "proxy": proxy,
            "status": "WORKING" if ok else "FAILED",
            "ms": int(elapsed * 1000),
            "code": r.status_code,
        }
    except requests.exceptions.InvalidSchema:
        return {"proxy": proxy, "status": "FAILED", "error": "MISSING_PYSOCKS"}
    except Exception as e:
        return {"proxy": proxy, "status": "FAILED", "error": str(e)[:80]}


def test_http_batch(
    proxies: List[Dict[str, str]],
    workers: int = 40,
    target_url: str = "http://example.com",
    success_key: str = "Example Domain",
    timeout: float = 6.0,
    early_stop: int = 0,
) -> List[Dict[str, str]]:
    """
    Concurrent HTTP check. If early_stop > 0, cancel remaining once we have enough WORKING.
    """
    if not proxies:
        return []
    log(
        f"HTTP check: {len(proxies)} proxies ({workers} workers, timeout={timeout}s"
        + (f", early_stop={early_stop}" if early_stop else "")
        + ")..."
    )
    working: List[Dict[str, str]] = []
    failed = 0
    missing_dep = False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(test_http_proxy, p, target_url, success_key, timeout): p
            for p in proxies
        }
        pending = set(futs.keys())
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED, timeout=1.0)
            for f in done:
                res = f.result()
                if res.get("error") == "MISSING_PYSOCKS":
                    missing_dep = True
                if res["status"] == "WORKING":
                    working.append(res["proxy"])
                    if len(working) <= 15 or len(working) % 25 == 0:
                        log(
                            f"  ✓ {res['proxy']['addr']:22} {res.get('ms','?')}ms  "
                            f"(total OK {len(working)})"
                        )
                else:
                    failed += 1

            finished = len(working) + failed
            if finished % 50 == 0 and finished:
                print(
                    f"\r[{ts()}] HTTP {finished}/{len(proxies)}  OK={len(working)}  fail={failed}",
                    end="",
                    flush=True,
                )

            if early_stop and len(working) >= early_stop:
                # cancel rest
                for f in pending:
                    f.cancel()
                log(f"Early-stop: reached {len(working)} local WORKING")
                break

    print()
    if missing_dep:
        log("PySocks missing — SOCKS HTTP tests fail. pip install pysocks  OR use --skip-local")
    log(f"HTTP WORKING: {len(working)}/{len(proxies)}")
    return working


def local_filter(
    proxies: List[Dict[str, str]],
    workers: int,
    skip_tcp: bool,
    skip_http: bool,
    target_url: str,
    success_key: str,
    http_timeout: float,
    tcp_timeout: float,
    early_stop: int,
) -> List[Dict[str, str]]:
    if not proxies:
        return []
    cur = proxies
    if not skip_tcp:
        cur = test_tcp_batch(cur, workers=min(workers * 2, 200), timeout=tcp_timeout)
    if skip_http:
        return cur
    return test_http_batch(
        cur,
        workers=workers,
        target_url=target_url,
        success_key=success_key,
        timeout=http_timeout,
        early_stop=early_stop,
    )


# ── pipeline helpers ──────────────────────────────────────────────

def append_working_file(proxies: List[Dict[str, str]], path: str = "working_proxies.txt") -> None:
    if not proxies:
        return
    with open(path, "a", encoding="utf-8") as f:
        for p in proxies:
            f.write(f"({p['type'].capitalize()}){format_for_ob2(p)}\n")


def write_lines(path: str, lines: List[str]) -> None:
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    log(f"Wrote {len(lines)} lines → {path}")


def run_ob2_validate(
    ob2: OpenBullet2Client,
    proxy_type: str,
    bots: int,
    check_url: str,
    success_key: str,
    timeout_ms: int,
    purge_dead: bool,
    max_ping: int,
    export_path: Optional[str],
    recheck_all: bool,
    max_wait_sec: float = 0,
) -> Dict[str, int]:
    before = ob2.count_by_status(proxy_type=proxy_type)
    log(
        f"Before check: working={before['working']} dead={before['notWorking']} "
        f"untested={before['untested']} total={before['total']}"
    )
    if before["total"] == 0:
        log("Group is empty — nothing to validate")
        return before

    # Scale bots for large groups (OB2 proxy-check is otherwise multi-hour)
    effective_bots = bots
    if recheck_all and before["total"] > 100:
        effective_bots = max(bots, min(80, before["total"] // 5))
        if effective_bots != bots:
            log(f"Scaling bots {bots} → {effective_bots} for full recheck of {before['total']}")

    job_id = ob2.run_check_job(
        bots=effective_bots,
        target_url=check_url,
        success_key=success_key,
        timeout_ms=timeout_ms,
        check_only_untested=not recheck_all,
    )
    if not job_id:
        raise SystemExit(
            "OB2 proxy-check job failed to start. Is OpenBullet2 running on "
            f"{ob2.base_url}? Check API key and /api/v1/job/proxy-check."
        )

    # Estimate: free proxies often ~10-30s each; give room unless user caps wait
    if max_wait_sec <= 0 and recheck_all:
        # rough: 425 proxies / 40 bots * 12s ≈ 2+ min; use generous default
        max_wait_sec = max(600.0, (before["total"] / max(effective_bots, 1)) * 20.0)
        log(f"Auto max-wait ≈ {max_wait_sec:.0f}s for full recheck")

    last = ob2.wait_for_job(job_id, max_wait_sec=max_wait_sec)
    log(f"Job final snapshot: { {k: last.get(k) for k in ('status','tested','total','working','notWorking','lastRunOutcome') if last} }")

    if purge_dead:
        ob2.purge_not_working()
        if max_ping > 0:
            ob2.purge_slow(max_ping=max_ping)

    counts = ob2.count_by_status(proxy_type=proxy_type)
    log(
        f"Group '{ob2.group_name}': working={counts['working']}  "
        f"dead={counts['notWorking']}  untested={counts['untested']}  "
        f"total={counts['total']}"
    )

    if export_path:
        lines = ob2.download_working(proxy_type=proxy_type)
        write_lines(export_path, lines)

    return counts


# ── main ──────────────────────────────────────────────────────────

def main() -> None:
    show_banner()
    
    # If no arguments provided, show help and exit
    if len(sys.argv) == 1:
        p = argparse.ArgumentParser(
            description="proxyreaper — fast multi-protocol proxy harvester + OB2 syncer",
            epilog="Examples:\n"
            "  python3 proxyreaper.py --type socks5 --OB2\n"
            "  python3 proxyreaper.py --type socks5 --OB2 --file resi.txt --skip-local --clear\n"
            "  python3 proxyreaper.py --type socks4 --OB2 --recheck-all --check-url https://example.com/ --success-key \"Example Domain\"\n"
            "  python3 proxyreaper.py --type socks5 --OB2 --workers 80 --bots 50 --target-count 100",
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.print_help()
        sys.exit(0)
    
    p = argparse.ArgumentParser(
        description="proxyreaper — fast multi-protocol proxy harvester + OB2 syncer",
        epilog="Examples:\n"
        "  python3 proxyreaper.py --type socks5 --OB2\n"
        "  python3 proxyreaper.py --type socks5 --OB2 --file resi.txt --skip-local --clear\n"
        "  python3 proxyreaper.py --type socks4 --OB2 --recheck-all --check-url https://example.com/ --success-key \"Example Domain\"\n"
        "  python3 proxyreaper.py --type socks5 --OB2 --workers 80 --bots 50 --target-count 100",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--type", default="socks5", choices=["http", "socks4", "socks5"])
    p.add_argument("--OB2", action="store_true", help="Sync + validate in OpenBullet2")
    p.add_argument("--group", default=None, help="OB2 group name (default by --type)")
    p.add_argument("--file", default=None, help="Proxy list file")
    p.add_argument("--skip-local", action="store_true", help="Skip local TCP+HTTP (residential)")
    p.add_argument("--skip-tcp", action="store_true", help="Skip TCP prefilter only")
    p.add_argument("--skip-http", action="store_true", help="TCP only locally, no HTTP")
    p.add_argument("--clear", action="store_true", help="Clear OB2 group before import")
    p.add_argument("--target-count", type=int, default=50, help="Stop when group has N working")
    p.add_argument("--batch-size", type=int, default=150, help="Free-list batch size")
    p.add_argument("--max-iterations", type=int, default=15)
    p.add_argument("--workers", type=int, default=50, help="Local HTTP worker threads")
    p.add_argument(
        "--bots",
        type=int,
        default=40,
        help="OB2 proxy-check bots (auto-scales up on full recheck of large groups)",
    )
    p.add_argument(
        "--max-wait",
        type=float,
        default=0,
        help="Max seconds to wait for OB2 proxy-check job (0=auto for recheck, forever otherwise)",
    )
    p.add_argument("--http-timeout", type=float, default=6.0)
    p.add_argument("--tcp-timeout", type=float, default=2.0)
    p.add_argument("--check-timeout-ms", type=int, default=8000)
    p.add_argument(
        "--check-url",
        default="http://example.com",
        help="URL OB2 hits when validating a proxy (generic; set to your target)",
    )
    p.add_argument(
        "--success-key",
        default="Example Domain",
        help="Substring that must appear in the check response for SUCCESS",
    )
    p.add_argument(
        "--no-purge",
        action="store_true",
        help="Do not delete notWorking after OB2 check",
    )
    p.add_argument(
        "--max-ping",
        type=int,
        default=10000,
        help="After check, delete working proxies slower than this ms (0=skip)",
    )
    p.add_argument(
        "--export",
        default="ob2_working_proxies.txt",
        help="Write working proxies here after OB2 check (empty string to disable)",
    )
    p.add_argument(
        "--recheck-all",
        action="store_true",
        help=(
            "OB2 rechecks entire group (not only untested). "
            "In free mode, if working already >= --target-count, still run one "
            "full revalidate instead of silently re-exporting stale labels."
        ),
    )
    p.add_argument(
        "--early-stop",
        type=int,
        default=0,
        help="Stop local HTTP once N working found (0=disabled; free mode uses target-count)",
    )
    p.add_argument("--api", default="http://localhost:8501")
    p.add_argument("--api-key", default="i2pphlburfxvc1ls5di97ahajfnd8ytw")
    args = p.parse_args()

    group_name = args.group or DEFAULT_GROUP_FOR_TYPE.get(args.type, "Default")
    ob2 = OpenBullet2Client(base_url=args.api, api_key=args.api_key, group_name=group_name)
    export_path = args.export if args.export else None
    purge_dead = not args.no_purge

    log(
        f"type={args.type} OB2={args.OB2} group='{group_name}' "
        f"file={args.file or '-'} skip_local={args.skip_local} "
        f"check_url={args.check_url!r} recheck_all={args.recheck_all}"
    )

    if args.OB2:
        if not ob2.get_group_id():
            raise SystemExit("Cannot resolve OB2 group")
        if args.clear:
            ob2.clear_proxies()

    # ── FILE MODE ────────────────────────────────────────────────────────
    if args.file:
        candidates = [
            x for x in load_proxies_from_file(args.file, args.type) if x["type"] == args.type
        ]
        if not candidates:
            raise SystemExit(f"No {args.type} proxies in {args.file}")

        if args.skip_local:
            survivors = candidates
            log(f"skip-local: {len(survivors)} candidates")
        else:
            early = args.early_stop or 0
            survivors = local_filter(
                candidates,
                workers=args.workers,
                skip_tcp=args.skip_tcp,
                skip_http=args.skip_http,
                target_url="http://example.com",
                success_key="Example Domain",
                http_timeout=args.http_timeout,
                tcp_timeout=args.tcp_timeout,
                early_stop=early,
            )

        append_working_file(survivors)

        if not args.OB2:
            log(f"Done (local only): {len(survivors)} kept")
            return

        # skip already-in-group
        existing = ob2.existing_keys(args.type)
        new_ones = [p for p in survivors if p["key"] not in existing]
        log(f"New to OB2: {len(new_ones)} (skip {len(survivors)-len(new_ones)} already present)")
        if new_ones:
            ob2.import_proxies([format_for_ob2(p) for p in new_ones], proxy_type=args.type)

        counts = run_ob2_validate(
            ob2,
            args.type,
            bots=args.bots,
            check_url=args.check_url,
            success_key=args.success_key,
            timeout_ms=args.check_timeout_ms,
            purge_dead=purge_dead,
            max_ping=args.max_ping,
            export_path=export_path,
            recheck_all=args.recheck_all,
            max_wait_sec=args.max_wait,
        )
        print("=" * 72)
        print(f"FINAL: '{group_name}' working={counts['working']} total={counts['total']}")
        print("=" * 72)
        return

    # ── FREE LIST MODE ──────────────────────────────────────────────────
    target = args.target_count
    iteration = 0
    tried: Set[str] = set()

    current = 0
    if args.OB2:
        c = ob2.count_by_status(args.type)
        current = c["working"]
        log(f"Group already working={current} total={c['total']}")

        # Avoid the "export stale working=N, tried=0" trap: if the group already
        # meets target, free harvest would skip entirely. Either revalidate or exit loud.
        if current >= target:
            if args.recheck_all:
                log(
                    f"Target already met ({current}>={target}); "
                    f"--recheck-all set → full OB2 revalidate (no free harvest this run)"
                )
                counts = run_ob2_validate(
                    ob2,
                    args.type,
                    bots=args.bots,
                    check_url=args.check_url,
                    success_key=args.success_key,
                    timeout_ms=args.check_timeout_ms,
                    purge_dead=purge_dead,
                    max_ping=args.max_ping,
                    export_path=export_path,
                    recheck_all=True,
                    max_wait_sec=args.max_wait,
                )
                print("\n" + "=" * 72)
                print(
                    f"FINAL: group '{group_name}' working={counts['working']} "
                    f"total={counts['total']}"
                )
                print(f"Tried unique free proxies this run: {len(tried)}")
                print(
                    "Note: revalidated existing group only "
                    f"(check_url={args.check_url!r})"
                )
                if export_path:
                    print(f"Working export: {export_path}")
                print("=" * 72)
                return
            log(
                f"Target already met ({current}>={target}). "
                f"Skipping free harvest and NOT rechecking. "
                f"Pass --recheck-all to revalidate, or raise --target-count to harvest more."
            )
            if export_path:
                lines = ob2.download_working(args.type)
                write_lines(export_path, lines)
                log(f"Exported {len(lines)} cached working labels → {export_path} (NOT retested)")
            print("\n" + "=" * 72)
            print(
                f"FINAL: group '{group_name}' working={current} total={c['total']} "
                f"(CACHED — no check this run)"
            )
            print("Tried unique free proxies this run: 0")
            if export_path:
                print(f"Working export: {export_path}")
            print("=" * 72)
            return

    while current < target and iteration < args.max_iterations:
        iteration += 1
        log(f"--- ITERATION {iteration}/{args.max_iterations} (need {target - current} more) ---")

        if args.type == "socks5":
            batch = fetch_socks5_parallel(limit=args.batch_size)
        elif args.type == "socks4":
            batch = fetch_socks4_from_web(limit=args.batch_size)
        else:
            # parallel HTTP sources
            with ThreadPoolExecutor(max_workers=3) as ex:
                futs = [
                    ex.submit(fetch_http_table, "https://free-proxy-list.net/", "FreeProxyList", args.batch_size // 3),
                    ex.submit(fetch_http_table, "https://www.ssl-proxies.org/", "SSLProxies", args.batch_size // 3),
                    ex.submit(fetch_http_table, "https://www.us-proxy.org/", "USProxy", args.batch_size // 3),
                ]
                batch = []
                for f in as_completed(futs):
                    batch.extend(f.result())

        batch = [p for p in batch if p["type"] == args.type and p["key"] not in tried]
        for p in batch:
            tried.add(p["key"])
        if not batch:
            log("Empty batch, continuing...")
            time.sleep(1)
            continue

        if args.skip_local:
            survivors = batch
        else:
            # free mode: early-stop local HTTP at remaining needed * 1.5 (OB2 will filter more)
            need = max(target - current, 10)
            early = args.early_stop or min(len(batch), int(need * 2))
            survivors = local_filter(
                batch,
                workers=args.workers,
                skip_tcp=args.skip_tcp,
                skip_http=args.skip_http,
                target_url="http://example.com",
                success_key="Example Domain",
                http_timeout=args.http_timeout,
                tcp_timeout=args.tcp_timeout,
                early_stop=early,
            )

        append_working_file(survivors)

        if not args.OB2:
            current += len(survivors)
            log(f"Local cumulative OK≈{current}/{target}")
            continue

        if not survivors:
            continue

        existing = ob2.existing_keys(args.type)
        new_ones = [p for p in survivors if p["key"] not in existing]
        log(f"Pushing {len(new_ones)} new (dedupe against group)")
        if not new_ones:
            # still may need a check if untested remain
            c = ob2.count_by_status(args.type)
            if c["untested"] > 0:
                run_ob2_validate(
                    ob2, args.type, args.bots, args.check_url, args.success_key,
                    args.check_timeout_ms, purge_dead, args.max_ping, None, False,
                )
            current = ob2.count_by_status(args.type)["working"]
            continue

        ob2.import_proxies([format_for_ob2(p) for p in new_ones], proxy_type=args.type)
        counts = run_ob2_validate(
            ob2,
            args.type,
            bots=args.bots,
            check_url=args.check_url,
            success_key=args.success_key,
            timeout_ms=args.check_timeout_ms,
            purge_dead=purge_dead,
            max_ping=args.max_ping,
            export_path=export_path if current + len(new_ones) >= target else None,
            recheck_all=args.recheck_all,
            max_wait_sec=args.max_wait,
        )
        current = counts["working"]
        if current >= target:
            log(f"SUCCESS: {current} working in '{group_name}'")
            break

    if args.OB2 and export_path:
        lines = ob2.download_working(args.type)
        write_lines(export_path, lines)

    print("\n" + "=" * 72)
    if args.OB2:
        c = ob2.count_by_status(args.type)
        print(f"FINAL: group '{group_name}' working={c['working']} total={c['total']}")
        print(f"Tried unique free proxies this run: {len(tried)}")
        if export_path:
            print(f"Working export: {export_path}")
    else:
        print(f"FINAL: local free-harvest (no OB2), tried={len(tried)}")
    print("=" * 72)


if __name__ == "__main__":
    main()