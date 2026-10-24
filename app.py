#!/usr/bin/env python3
"""Khan Research & Data — x402 paid data service.

Paid endpoints (x402 v2, scheme=exact, USDC on Base, facilitator = PayAI):
  GET /v1/versions   $0.05  latest stable releases of major OSS tools (live)
  GET /v1/fx-crypto  $0.02  BTC/ETH/SOL prices + USD FX rates (live)

Free endpoints:
  GET /health        liveness + config echo
  GET /              service index (routes + prices, for humans/indexers)
  POST /mcp          minimal MCP (Streamable HTTP) surface exposing the same
                     two data functions as tools — this is the surface a
                     marketplace gateway (FiatDock) monetises; it is NOT
                     x402-gated here on purpose.

TEST MODE: if env X402_TEST_MODE=1, the payment middleware is NOT installed
and paid routes serve data directly (responses carry X-Test-Mode: bypassed).
OFF by default; never set it in production units.
"""
import json
import os
import sys

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as datalayer  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "config.json")) as f:
    CFG = json.load(f)

TEST_MODE = os.environ.get("X402_TEST_MODE") == "1"

# Testnet override (used only by the local settlement test instance):
# X402_NETWORK=eip155:84532 makes the middleware issue Base-Sepolia
# requirements (asset defaults resolve to Sepolia USDC in the SDK).
if os.environ.get("X402_NETWORK"):
    CFG["network"] = os.environ["X402_NETWORK"]
    CFG["network_name"] = "base-sepolia"

app = Flask(__name__)


# --------------------------------------------------------------------- free
@app.get("/health")
def health():
    return jsonify(
        status="ok",
        service=CFG["service_name"],
        network=CFG["network"],
        pay_to=CFG["pay_to"],
        facilitator=CFG["facilitator_url"],
        test_mode=TEST_MODE,
    )


@app.get("/")
def index():
    routes = [
        {
            "method": "GET",
            "path": path,
            "price_usd": rc["price_usd"],
            "asset": "USDC",
            "network": CFG["network"],
            "pay_to": CFG["pay_to"],
            "description": rc["description"],
        }
        for path, rc in CFG["routes"].items()
    ]
    return jsonify(service=CFG["service_name"], protocol="x402",
                   x402_version=2, paid_routes=routes)


# --------------------------------------------------------------------- paid
def _serve(fn, key):
    try:
        payload = fn()
    except Exception as e:  # upstream failure -> 502, no charge semantics here
        return jsonify(error="upstream_fetch_failed",
                       detail=str(e)[:200]), 502
    resp = jsonify(service=key, **payload)
    if TEST_MODE:
        resp.headers["X-Test-Mode"] = "bypassed"
    return resp


@app.get("/v1/versions")
def versions():
    return _serve(datalayer.get_versions, "versions")


@app.get("/v1/fx-crypto")
def fx_crypto():
    return _serve(datalayer.get_fx_crypto, "fx-crypto")


# ---------------------------------------------------------------------- mcp
MCP_TOOLS = [
    {
        "name": "latest_versions",
        "description": "Latest stable release info for Node.js, Python, Go, "
                       "Rust, Git, Kubernetes and Docker Engine, fetched live "
                       "from official GitHub releases.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fx_crypto",
        "description": "Current BTC/ETH/SOL spot prices (USD/EUR, 24h change) "
                       "plus USD FX rates, fetched live from public APIs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


@app.post("/mcp")
def mcp():
    body = request.get_json(silent=True) or {}
    method = body.get("method", "")
    rid = body.get("id")

    def reply(result):
        return jsonify(jsonrpc="2.0", id=rid, result=result)

    def error(code, message):
        return jsonify(jsonrpc="2.0", id=rid,
                       error={"code": code, "message": message})

    if method == "initialize":
        return reply({
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "khan-research-data", "version": "1.0.0"},
        })
    if method == "ping":
        return reply({})
    if method.startswith("notifications/"):
        return ("", 204)
    if method == "tools/list":
        return reply({"tools": MCP_TOOLS})
    if method == "tools/call":
        name = (body.get("params") or {}).get("name")
        fn = {"latest_versions": datalayer.get_versions,
              "fx_crypto": datalayer.get_fx_crypto}.get(name)
        if fn is None:
            return error(-32602, f"unknown tool: {name}")
        try:
            payload = fn()
        except Exception as e:
            return error(-32000, f"upstream fetch failed: {str(e)[:160]}")
        return reply({
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False,
        })
    return error(-32601, f"method not found: {method}")


@app.get("/mcp")
def mcp_get():
    return jsonify(error="method_not_allowed",
                   detail="MCP endpoint is POST-only (stateless)"), 405


# ------------------------------------------------------- payment middleware
if not TEST_MODE:
    from x402 import x402ResourceServerSync
    from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
    from x402.http.middleware.flask import payment_middleware
    from x402.http.types import PaymentOption, RouteConfig
    from x402.mechanisms.evm.exact.register import register_exact_evm_server

    # httpx crashes parsing this VM's NO_PROXY (bracketed IPv6 entries), so
    # hand the facilitator an explicit client: proxy from env, trust_env off.
    import httpx

    _proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
              or None)
    _http_client = httpx.Client(timeout=30.0, proxy=_proxy, trust_env=False,
                                follow_redirects=True)
    facilitator = HTTPFacilitatorClientSync(
        FacilitatorConfig(url=CFG["facilitator_url"],
                          http_client=_http_client))
    server = x402ResourceServerSync(facilitator)
    register_exact_evm_server(server, CFG["network"])

    routes = {}
    for path, rc in CFG["routes"].items():
        routes[f"GET {path}"] = RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=CFG["pay_to"],
                price=f"${rc['price_usd']}",
                network=CFG["network"],
            ),
            description=rc["description"],
            mime_type=rc["mime_type"],
            service_name="Khan Research & Data",
            tags=["data", "research", "live-data"],
        )
    payment_middleware(app, routes, server)
else:
    app.logger.warning("X402_TEST_MODE=1 — payment middleware DISABLED")


if __name__ == "__main__":
    app.run(host=CFG.get("host", "127.0.0.1"),
            port=int(os.environ.get("X402_PORT", CFG.get("port", 8402))))
