
![ProxyReaper Banner](banner.png)

**ProxyReaper** is a blazing-fast proxy harvester and validator with integrated OpenBullet2 synchronization. Harvest proxies from the wild, validate them locally or in OB2, and export your working proxy list—all in one tool.

## Features

- **Dual-Mode Validation**: Fast local HTTP/TCP prefiltering + precision validation in OpenBullet2
- **Smart Batch Processing**: Configure workers, bot threads, and batch sizes for optimal throughput
- **Flexible Filtering**: Skip local checks, validate only TCP, or go full HTTP—your choice
- **OB2 Integration**: Sync directly to OB2 groups, auto-scale validation, handle proxy labels
- **Timeout Control**: Granular control over TCP, HTTP, and OB2 check timeouts
- **Purge & Prune**: Auto-delete non-working proxies, filter by ping time post-validation
- **Early Exit**: Stop hunting once you've got enough working proxies
- **Export & Revalidate**: Export working lists or full recheck groups for freshness

## Installation

```bash
git clone https://github.com/garybense/proxyreaper.git
cd proxyreaper
pip install -r requirements.txt
```

### Dependencies

- `requests` — HTTP client
- `urllib3` — Connection pooling
- `pyfiglet` — ASCII banner rendering
- `pysocks` — Required for SOCKS proxy support (install via `pip install pysocks`)

### Free Proxy Sources

**SOCKS5:**
- TheSpeedX/PROXY-List (GitHub)
- mmpx12/proxy-list (GitHub)
- ProxyScrape API
- Geonode API
- (Others as configured)

**SOCKS4:**
- TheSpeedX/PROXY-List (GitHub)
- mmpx12/proxy-list (GitHub)
- ProxyScrape API
- Geonode API
- socks-proxy.net

**HTTP:**
- free-proxy-list.net
- ssl-proxies.org
- us-proxy.org

## Workflow Modes

**File Mode** (with `--file`)
- Load proxies from a file you already have (residential, datacenter, whatever)
- Validate locally (or skip with `--skip-local`)
- Optionally sync to OB2 for precision validation
- Single pass, no auto-harvesting

**Free Harvest Mode** (without `--file`)
- Auto-harvest from free proxy sources (multiple per protocol)
- Batch validation locally (TCP → HTTP filter)
- Iteratively fetch new batches until target is met
- Sync each batch to OB2 for validation
- Best for hunting large pools of working proxies

---

## Quick Start

### Basic proxy validation (local only)

```bash
python3 proxy_tester.py --type http --file proxies.txt
```

### Validate with OpenBullet2

```bash
python3 proxy_tester.py --type socks5 --OB2
```

### Hunt for 100 working SOCKS5 proxies and export

```bash
python3 proxy_tester.py --type socks5 --OB2 --workers 80 --bots 50 --target-count 100 --export working.txt
```

### Recheck an entire OB2 group (force full validation)

```bash
python3 proxy_tester.py --type socks4 --OB2 --recheck-all --check-url https://example.com/ --success-key "Example Domain"
```

### Fast triage with TCP only, skip HTTP

```bash
python3 proxy_tester.py --type http --file resi.txt --skip-http
```

### Skip local checks, validate only in OB2

```bash
python3 proxy_tester.py --type socks5 --OB2 --file proxies.txt --skip-local --clear
```

## Options Reference

### Proxy Type
- `--type {http,socks4,socks5}` — Proxy protocol (default: `socks5`)

### OpenBullet2 Integration
- `--OB2` — Enable OpenBullet2 sync and validation
- `--api API` — OpenBullet2 API endpoint (default: `http://localhost:8501`)
- `--api-key API_KEY` — OpenBullet2 API key (default: `i2pphlburfxvc1ls5di97ahajfnd8ytw`)
- `--group GROUP` — OB2 group name (defaults to type if unset: `http`/`socks4`/`socks5`)
- `--clear` — Clear the OB2 group before import
- `--bots BOTS` — Number of proxy-check bots in OB2 (default: 40; auto-scales on full recheck)

### Input & Output
- `--file FILE` — Proxy list file (read line-by-line). Supports formats:
  - `host:port`
  - `host:port:user:pass`
  - `user:pass@host:port`
  - `(type) host:port` (e.g., `(socks5) 127.0.0.1:9050`)
  - `socks5://host:port`
  - `(Socks5)host:port:user:pass`
- `--export EXPORT` — Export working proxies to file after OB2 check (default: `ob2_working_proxies.txt`; empty string to disable)

### Local Validation (Pre-filtering)
- `--skip-local` — Skip all local validation (TCP + HTTP)
- `--skip-tcp` — Skip TCP prefilter, go straight to HTTP
- `--skip-http` — TCP only, don't validate HTTP locally
- `--workers WORKERS` — Number of HTTP validation threads (default: 50)
- `--http-timeout HTTP_TIMEOUT` — Timeout per HTTP request in seconds (default: 6.0)
- `--tcp-timeout TCP_TIMEOUT` — Timeout per TCP connection in seconds (default: 2.0)

### OB2 Validation
- `--check-url CHECK_URL` — URL for OB2 to hit when validating (default: `http://example.com`)
- `--success-key SUCCESS_KEY` — Substring that must appear in response for a proxy to pass (default: `"Example Domain"`)
- `--check-timeout-ms CHECK_TIMEOUT_MS` — OB2 check timeout in milliseconds (default: 8000)
- `--max-wait MAX_WAIT` — Max seconds to wait for OB2 job (0 = auto for recheck, infinite otherwise; default: 0)
- `--recheck-all` — Force OB2 to recheck entire group (not just untested). In free mode, if working ≥ `--target-count`, runs one full revalidate

### Filtering & Limits
- `--target-count TARGET_COUNT` — Stop once the OB2 group has N working proxies (default: 50)
- `--early-stop EARLY_STOP` — Stop local HTTP hunting once N proxies found (0 = disabled; free mode uses `--target-count`)
- `--max-ping MAX_PING` — Delete working proxies slower than this ping (ms; default: 10000). Set to 0 to skip
- `--no-purge` — Keep non-working proxies in OB2 after check (default: delete them)

### Batch & Performance
- `--batch-size BATCH_SIZE` — Proxy list batch size for free-list (default: 150)
- `--max-iterations MAX_ITERATIONS` — Maximum iteration count (default: 15)

### Advanced
- `--api API` — Custom API endpoint
- `--api-key API_KEY` — API key for authentication

## Examples

**Harvest 50 HTTP proxies locally, no OB2:**
```bash
python3 proxy_tester.py --type http --file all_proxies.txt --workers 40 --early-stop 50 --export http_working.txt
```

**Validate SOCKS4 in OB2, skip local checks, clear group first:**
```bash
python3 proxy_tester.py --type socks4 --OB2 --file socks4_list.txt --skip-local --clear --target-count 200
```

**Full revalidation with custom success condition:**
```bash
python3 proxy_tester.py --type socks5 --OB2 --recheck-all \
  --check-url https://httpbin.org/ip \
  --success-key "origin" \
  --bots 100 \
  --max-ping 500 \
  --export validated_socks5.txt
```

**Aggressive hunt: max workers, max bots, tight timeouts:**
```bash
python3 proxy_tester.py --type socks5 --OB2 --workers 128 --bots 200 \
  --http-timeout 3 --tcp-timeout 2 --target-count 500 --export socks5_bulk.txt
```

## Workflow Tips

1. **Start broad, filter tight**: Use `--skip-http` for initial triage on large batches, add HTTP validation later.
2. **Use `--early-stop`** to exit local validation as soon as you have enough proxies (speeds up free harvesting).
3. **Leverage `--recheck-all`** periodically to keep your OB2 group fresh and validate against new targets.
4. **Set `--max-ping`** to prune slow proxies post-validation—better a smaller working set than bloated latency.
5. **Combine `--clear` + `--skip-local`** for pure OB2-side validation on large imports (trust OB2 precision).
6. **Use `--check-url` + `--success-key`** to validate against your actual target site, not just generic example.com.
7. **Monitor free source stability**: Some free sources are flakier than others; adjust `--batch-size` if needed.
8. **For residential proxies**: Load with `--file`, use `--skip-local`, go straight to OB2 (residential IPs are slow to TCP-test).
9. **Tune `--workers` and `--bots`** based on your system (more = faster but higher CPU; experiment).
10. **Export working lists regularly** with `--export`; don't rely on OB2 alone for persistence.

## Common Scenarios

**Quick validation of a residential proxy list:**
```bash
python3 proxyreaper.py --type socks5 --OB2 --file resi.txt --skip-local \
  --check-url https://your-api.com/check --success-key "success" --export validated.txt
```

**Hunt for 500 free SOCKS5 (max speed):**
```bash
python3 proxyreaper.py --type socks5 --OB2 --workers 100 --bots 80 \
  --target-count 500 --batch-size 200 --early-stop 100 --export socks5_bulk.txt
```

**Safe mode: Small batch, strict validation:**
```bash
python3 proxyreaper.py --type http --OB2 --batch-size 50 \
  --target-count 25 --max-ping 2000 --check-url https://example.com --success-key "Example"
```

**Recheck and export (no harvesting):**
```bash
python3 proxyreaper.py --type socks4 --OB2 --skip-tcp --skip-http --recheck-all \
  --bots 50 --export socks4_validated.txt
```

## Requirements

- Python 3.8+
- OpenBullet2 running locally or remote (if using `--OB2`)
- Network access to validation targets
- `pip install -r requirements.txt`:
  - `requests>=2.28.0`
  - `urllib3>=1.26.0`
  - `pyfiglet>=0.8.0`
  - `pysocks>=1.7.1` (for SOCKS proxy support)

## License

MIT

---

**ProxyReaper** — because sometimes you gotta reap what you sow.
