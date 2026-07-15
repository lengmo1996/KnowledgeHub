# KnowledgeHub Read-only MCP Threat Model

## Overview

KnowledgeHub indexes a local Zotero-derived academic corpus in Qdrant and exposes retrieval through CLI, REST,
and this new read-only MCP surface. The MCP implementation is intentionally an adapter over
`RetrievalService` (`src/knowledgehub/retrieval/service.py`); it does not expose sync, parse, chunk, embedding
build, Qdrant mutation, arbitrary SQL, file access, URL fetching, resources, prompts, or write tools.

The primary production surfaces are a trusted-LAN HTTP listener at `10.249.44.27:8091`, restricted by UFW to
`10.249.43.193`, and a loopback backend at `127.0.0.1:8092` published through Tailscale Serve HTTPS. STDIO uses
the same low-level server factory. Assets include per-device bearer credentials, the external token HMAC key,
Zotero metadata and document text, bibliographic privacy, Qdrant availability, GPU embedding availability,
audit integrity, and the host's existing remote-management path.

The deployed service is expected to run as non-root `lengmo`; only the LAN systemd preflight executes with
restricted root authority to inspect the interface, port, and exact UFW rules. Source documents are untrusted
data even when they came from the operator's Zotero library.

## Threat Model, Trust Boundaries, and Assumptions

| Boundary | Less-trusted side | More-trusted side | Security invariant |
|---|---|---|---|
| LAN client → LAN listener | One allowed workstation, LAN attackers | MCP process | Exact source IP plus bearer auth; no forwarded-header trust |
| Tailnet client → Serve → loopback | Tailnet identities and client input | MCP backend | HTTPS at Serve; backend loopback-only; only loopback proxy may supply XFF |
| MCP protocol → tool registry | JSON-RPC names and arguments | Seven handlers | Strict schemas, fixed registry, read-only annotations, no dynamic dispatch |
| Retrieved text → model | PDFs, metadata, malicious paper text | Client reasoning | Content is labeled untrusted and never becomes an instruction |
| MCP process → token store | File replacement or malformed JSON | Last valid token snapshot | HMAC hashes, constant-time compare, mode 0600, safe reload and degraded readiness |
| MCP process → Qdrant/TEI/reranker | Dependency failure or hostile payload data | Bounded tool response | Loopback endpoints, deadlines, concurrency bounds, circuit state, response cap |
| MCP process → manifest/SQLite | Operator-produced state | Read-only catalog | Fixed configured paths, SQLite `mode=ro`, `query_only`, no user SQL or path |
| Operator → deployment scripts | Root shell and current firewall/policy | Host availability | Dry-run, explicit confirmation strings, backup, verification, isolated rollback |

Attacker-controlled inputs include bearer strings, HTTP headers, JSON-RPC messages, tool parameters, search
queries, and document content already present in the corpus. Operator-controlled inputs include environment files,
token records, listener mode, Tailscale policy merge, UFW approval, and fixed data paths. Developer-controlled
inputs include Python dependencies, schemas, systemd units, and deployment scripts.

Assumptions: the LAN is trusted only for confidentiality of cleartext traffic, not for authentication; Tailscale
coordination and HTTPS certificates are trusted; root protects `/etc/knowledgehub`; Qdrant, TEI, and reranker stay
loopback-only; the host itself and the `lengmo` account are not already compromised. A malicious authenticated
client is in scope for authorization, availability, and corpus-exfiltration analysis. Physical host compromise,
malicious root, Tailscale control-plane compromise, and attacks requiring arbitrary replacement of trusted source
code are out of scope because they already cross the highest trust boundary.

## Attack Surface, Mitigations, and Attacker Stories

| Attack path | Impact | Controls and tests | Residual risk |
|---|---|---|---|
| Stolen/replayed bearer token | Corpus disclosure and service load | Per-device token, expiry/disable/rotate, HMAC-SHA-256 at rest, constant-time compare, path/CIDR checks, token tests | LAN HTTP exposes bearer to a capable on-path observer; use Tailnet HTTPS for sensitive networks |
| Session ID reused with a second principal | Cross-device session confusion | SDK stateful session owner binding; protocol test changes token on existing session | A stolen token plus session ID remains equivalent to that principal |
| DNS rebinding or hostile browser Origin | Requests reach loopback/backend unexpectedly | SDK Host and Origin allowlists; absent Origin allowed for non-browser clients; protocol tests | Host allowlists must be updated intentionally when DNS names change |
| Forged XFF/Tailscale identity headers | CIDR bypass or false audit attribution | LAN ignores all forwarded headers; Tailnet backend reads XFF only from loopback; identity is audit-only | A compromised local proxy is inside the backend trust boundary |
| Prompt injection embedded in a paper | Model follows retrieved malicious instructions | Server instructions, content origin/trust labels, warning detector, tests retaining but distrusting text | Detection is heuristic; the consuming model must enforce the trust label |
| Arbitrary filter/path/URL/SQL input | File disclosure, SSRF, broad corpus query, SQL injection | Pydantic extra-forbid at every object, enumerated filters/facets, fixed paths and parameterized SQL, tests | Broad semantic queries can still retrieve authorized corpus data |
| DOI/title ambiguity abused to select wrong work | Integrity error in downstream answer | `ambiguous` response with candidates; no silent choice | Similar titles require client/user disambiguation |
| Oversized queries, neighbor fan-out, slow dependencies | GPU/Qdrant exhaustion and listener starvation | Length/count bounds, semaphore, per-principal/IP sliding window, deadlines, 1 MiB cap, bounded neighbors; async HTTP/Qdrant calls propagate cancellation | Local sparse encoding and manifest/SQLite reads are brief synchronous sections and can delay cancellation until they return |
| Qdrant/embedding/reranker outage | Loss of availability or incorrect fallback | Explicit strict/degrade policy, hybrid→sparse warning, reranker fallback field, separate units and circuit state | Sparse itself depends on local model cache and Qdrant; simultaneous failure is not recoverable |
| Malformed token-store hot reload | Lockout or unintended credential acceptance | Atomic admin writes, last-good snapshot, readiness degraded, reload test | Revocation is delayed while a malformed replacement remains unresolved |
| Log injection or secret leakage | Audit corruption and credential exposure | Structured JSON audit fields, bounded/sanitized metadata, no auth header/tool body, secret-free status | Journal administrators and root can still read operational metadata |
| UFW/systemd/Serve misconfiguration | Public exposure or remote lockout | Dry-run, exact confirm strings, UFW backup/commented rollback, loopback bind, Funnel check, hardened units | Operator may bypass scripts or merge an overbroad tailnet policy |
| One listener crashes | Partial service loss | Separate LAN and Tailscale units, independent restart and audit logs | Shared Qdrant/TEI failures affect both listeners |

The highest-value review areas are `src/knowledgehub/mcp/tokens.py`, HTTP middleware ordering in
`src/knowledgehub/mcp/runtime.py`, strict schemas in `src/knowledgehub/mcp/schemas.py`, catalog path/SQL handling in
`src/knowledgehub/mcp/catalog.py`, and UFW/Tailscale deployment assets. Typical web XSS and CSRF are lower priority:
the service returns JSON/SSE rather than HTML, requires Authorization, and validates Origin. Multi-tenant object
isolation is not claimed; possession of an authorized token grants read access to the configured corpus.

## Severity Calibration (Critical, High, Medium, Low)

**Critical.** A remotely reachable, unauthenticated path to execute commands as `lengmo` or root; an MCP tool that
can overwrite Qdrant/pipeline/Zotero state; or a deployment action that reliably exposes the listener through
Funnel to the public Internet with no authentication. These compromise the host or the full corpus across the
strongest boundary.

**High.** Remote bearer-auth bypass; arbitrary file read (especially `/etc/knowledgehub` secrets); SSRF that reaches
loopback admin services; cross-principal session takeover; or a raw SQL/filter interface that enables access beyond
the configured corpus. A LAN-only cleartext token capture is High when an attacker is realistically on path, but it
does not make the service Critical because LAN cleartext is an explicit deployment assumption and Tailnet HTTPS is
the required alternative.

**Medium.** Authenticated denial of service that exhausts embedding/Qdrant capacity despite bounds; forwarded-header
spoofing that defeats CIDR policy without bypassing bearer auth; persistent audit-log injection; silent hybrid or
reranker fallback that materially misrepresents result quality; or malformed hot reload that delays revocation.
Impact is important but normally limited to availability, attribution, or one authorized corpus.

**Low.** Sanitized version/listener disclosure, a slightly inaccurate facet count, harmless schema-description drift,
or a prompt-injection warning false positive. UI-only XSS, cross-site form CSRF, and public-registration abuse are not
applicable without a browser UI, cookies, or registration surface. Findings that require malicious root or arbitrary
source-code replacement are out of scope unless they create a separate persistence or privilege boundary violation.

Repository: /home/lengmo/KnowledgeHub
Version: 73fed34ebb900751562b3d6a4376c66ce8e5a131
