# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub's private vulnerability reporting](https://github.com/HakujouRyu/jobboard-mcp/security/advisories/new),
not in a public issue. Include what you found, how to reproduce it, and what an attacker could do
with it.

You should hear back within a week. Once a fix is out, the advisory is published with credit to
you, unless you'd rather not be named.

## Supported versions

Only the latest release gets fixes. The project is pre-1.0, so a fix may also change behavior.

## What's in scope

The server reads public job-board APIs and writes only its own watchlist file. The areas most
worth a look:

- **Outbound requests.** `resolve_board` and `find_company_posting` fetch a careers URL that the
  MCP client supplies, plus up to two same-site links from that page. These must stay on public
  addresses (SSRF).
- **Watchlist writes.** `add_company` and `update_company` edit `data/watchlist.yaml` from client
  input. Anything that writes outside that file, or leaves it invalid, is a bug.
- **Parsing untrusted data.** Board responses, careers-page HTML, and the watchlist YAML all come
  from outside the server.
- **Read-only mode.** With `WATCHLIST_READONLY=true`, no tool should be able to change anything.

Careers-page fetches are restricted to public addresses, checked on every redirect and pinned to
the checked address (see [docs/architecture.md](docs/architecture.md#careers-pages)). A way
around that check is a vulnerability.

## Known limitations

- There is no authentication. Only the stdio transport exists today, and it trusts its client.
