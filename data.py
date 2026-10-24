"""Live-data layer for the Khan x402 data service.

Fetches REAL data at request time from official/public sources:
- /v1/versions  -> GitHub Releases API (official repos)
- /v1/fx-crypto -> CoinGecko public API (crypto) + Frankfurter (FX, ECB data)

Results are cached briefly to respect upstream rate limits.
Outbound requests honour the standard proxy env vars (HTTP_PROXY/HTTPS_PROXY)
via `requests`.
"""
import time
import threading
import requests

UA = {"User-Agent": "khan-x402-data-service/1.0 (+https://github.com/amirghasemi2005z-oss)"}

_cache = {}
_lock = threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


# ---------------------------------------------------------------- versions
VERSION_TOOLS = [
    ("Node.js", "nodejs/node"),
    ("Python", "python/cpython"),
    ("Go", "golang/go"),
    ("Rust", "rust-lang/rust"),
    ("Git", "git/git"),
    ("Kubernetes", "kubernetes/kubernetes"),
    ("Docker Engine", "moby/moby"),
]


import re

_SEMVER_TAG = re.compile(r"^[a-zA-Z]*?(\d+)\.(\d+)\.(\d+)$")


def _latest_from_tags(repo):
    """Fallback for repos without GitHub Releases (e.g. python/cpython):
    pick the highest stable semver tag from the official repo."""
    r = requests.get(
        f"https://api.github.com/repos/{repo}/tags",
        params={"per_page": 100}, headers=UA, timeout=20,
    )
    r.raise_for_status()
    best = None
    for t in r.json():
        m = _SEMVER_TAG.match(t.get("name", ""))
        if m:
            key = tuple(int(x) for x in m.groups())
            if best is None or key > best[0]:
                best = (key, t["name"])
    if best is None:
        raise RuntimeError(f"no semver tags found for {repo}")
    tag = best[1]
    return {
        "latest_version": tag,
        "release_name": tag,
        "published_at": None,
        "release_url": f"https://github.com/{repo}/tree/{tag}",
        "info_source": "github_tags (repo publishes no GitHub Releases)",
    }


def _fetch_go_official():
    """Go publishes no GitHub releases and its tag list is unusable for
    latest-version lookup; the official go.dev downloads API is the source."""
    r = requests.get("https://go.dev/dl/?mode=json", headers=UA, timeout=20)
    r.raise_for_status()
    for rel in r.json():
        if rel.get("stable"):
            v = rel["version"]
            return {
                "latest_version": v,
                "release_name": v,
                "published_at": None,
                "release_url": f"https://go.dev/dl/#{v}",
                "info_source": "go.dev official downloads API",
            }
    raise RuntimeError("no stable Go release found")


def _fetch_one_version(repo):
    if repo == "golang/go":
        return _fetch_go_official()
    r = requests.get(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers=UA, timeout=20,
    )
    if r.status_code == 404:
        return _latest_from_tags(repo)
    r.raise_for_status()
    d = r.json()
    return {
        "latest_version": d.get("tag_name"),
        "release_name": d.get("name"),
        "published_at": d.get("published_at"),
        "release_url": d.get("html_url"),
        "info_source": "github_releases",
    }


def _fetch_versions():
    tools = []
    ok = 0
    for name, repo in VERSION_TOOLS:
        base = {"tool": name, "repo": repo,
                "source": f"https://github.com/{repo}/releases"}
        try:
            entry = _fetch_one_version(repo)
            ok += 1
        except Exception as e:  # per-tool failure -> transparent error entry
            entry = {"latest_version": None, "release_name": None,
                     "published_at": None, "release_url": None,
                     "info_source": "error", "error": str(e)[:120]}
        tools.append({**base, **entry})
    if ok == 0:
        raise RuntimeError("all version lookups failed")
    return {
        "data": {"tools": tools},
        "meta": {
            "source": "GitHub Releases API (official repositories)",
            "retrieved_at": _now_iso(),
        },
    }


def get_versions():
    return _cached("versions", 600, _fetch_versions)


# ---------------------------------------------------------------- fx/crypto
def _fetch_fx_crypto():
    cg = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={
            "ids": "bitcoin,ethereum,solana",
            "vs_currencies": "usd,eur",
            "include_24hr_change": "true",
        },
        headers=UA, timeout=20,
    )
    cg.raise_for_status()
    prices = cg.json()

    fx = requests.get(
        "https://api.frankfurter.app/latest",
        params={"from": "USD", "to": "EUR,GBP,JPY,CHF,AED,TRY"},
        headers=UA, timeout=20,
    )
    fx.raise_for_status()
    fxd = fx.json()

    sym = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}
    crypto = {}
    for cid, s in sym.items():
        p = prices.get(cid, {})
        crypto[s] = {
            "usd": p.get("usd"),
            "eur": p.get("eur"),
            "change_24h_pct": (round(p["usd_24h_change"], 3)
                               if p.get("usd_24h_change") is not None else None),
        }
    return {
        "data": {
            "crypto": crypto,
            "fx": {"base": "USD", "rates": fxd.get("rates", {}),
                   "date": fxd.get("date")},
        },
        "meta": {
            "sources": ["CoinGecko public API", "Frankfurter (ECB reference rates)"],
            "retrieved_at": _now_iso(),
        },
    }


def get_fx_crypto():
    return _cached("fx_crypto", 60, _fetch_fx_crypto)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
