# Khan Research & Data — x402 Data Service (Vercel deploy)

Paid data API over the x402 v2 protocol (HTTP 402 + USDC on Base, PayAI
keyless facilitator). Deployed on Vercel Hobby (free) so the service has a
stable public HTTPS endpoint.

- `GET /health` — free, liveness + config echo
- `GET /v1/versions` — $0.05, latest stable releases of major OSS tools
- `GET /v1/fx-crypto` — $0.02, BTC/ETH/SOL spot + USD FX rates
- `POST /mcp` — free MCP facade over the same datasets

Payments settle to the agent wallet on Base (public address in
`config.json`). No secrets live in this repo.
