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
  MCP client supplies, plus up to two same-site links from that page. A server that other people
  can reach could be pointed at internal addresses (SSRF).
- **Watchlist writes.** `add_company` and `update_company` edit `data/watchlist.yaml` from client
  input. Anything that writes outside that file, or leaves it invalid, is a bug.
- **Parsing untrusted data.** Board responses, careers-page HTML, and the watchlist YAML all come
  from outside the server.
- **Read-only mode.** With `WATCHLIST_READONLY=true`, no tool should be able to change anything.

## Known limitations

- Careers-page fetches are not yet blocked from private, loopback, or link-local addresses.
  This is fine for the local stdio server, where the person running it controls the client. It
  has to be fixed before the HTTP transport (milestone M3) ships.
- There is no authentication. Only the stdio transport exists today, and it trusts its client.
