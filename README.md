# kubecon-agenda-mcp

A read-only [MCP](https://modelcontextprotocol.io) server that lets AI agents search, browse and
sanity-check a KubeCon + CloudNativeCon schedule, co-located events included.

Default event: **KubeCon + CloudNativeCon North America 2026**, Salt Lake City, Nov 9-12.
Point it at any other conference that publishes a [Sessionize](https://sessionize.com) schedule
by changing one environment variable.

Community project. Not affiliated with CNCF, the Linux Foundation or Sessionize.

## Use it

Hosted endpoint (Streamable HTTP, no auth, read-only):

```
https://kubecon-schedule-mcp.slashben.net/mcp
```

It works with any MCP client that speaks Streamable HTTP. Pick yours:

<details open>
<summary><b>Claude Code</b></summary>

```sh
claude mcp add --transport http kubecon https://kubecon-schedule-mcp.slashben.net/mcp
```
</details>

<details>
<summary><b>Claude.ai / Claude Desktop</b></summary>

Settings, Connectors, Add custom connector, paste the URL. Available on paid plans.
</details>

<details>
<summary><b>ChatGPT</b> (Plus and above)</summary>

Settings, Apps & Connectors, Advanced settings, enable **Developer mode**. Then Connectors, Create,
paste the URL, choose *No authentication*. In a chat, open the plus menu and enable the connector.
Developer mode is a beta feature on OpenAI's side.
</details>

<details>
<summary><b>GitHub Copilot in VS Code</b></summary>

Add to `.vscode/mcp.json` (workspace) or run *MCP: Open User Configuration* (all projects):

```json
{
  "servers": {
    "kubecon": { "type": "http", "url": "https://kubecon-schedule-mcp.slashben.net/mcp" }
  }
}
```

Or from the shell: `code --add-mcp '{"name":"kubecon","type":"http","url":"https://kubecon-schedule-mcp.slashben.net/mcp"}'`.
Use it from Copilot Chat in agent mode.
</details>

<details>
<summary><b>GitHub Copilot CLI</b></summary>

Run `/mcp add` inside the CLI, or add to `~/.copilot/mcp-config.json`:

```json
{
  "mcpServers": {
    "kubecon": { "type": "http", "url": "https://kubecon-schedule-mcp.slashben.net/mcp", "tools": ["*"] }
  }
}
```
</details>

<details>
<summary><b>Gemini CLI</b></summary>

```sh
gemini mcp add --transport http -s user kubecon https://kubecon-schedule-mcp.slashben.net/mcp
```

or in `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "kubecon": { "httpUrl": "https://kubecon-schedule-mcp.slashben.net/mcp", "timeout": 30000 }
  }
}
```

Use `httpUrl`, not `url` (that one is for the older SSE transport). Check with `/mcp`.
</details>

<details>
<summary><b>OpenAI Codex CLI</b></summary>

```sh
codex mcp add kubecon --url https://kubecon-schedule-mcp.slashben.net/mcp
```
</details>

<details>
<summary><b>Cursor</b></summary>

Add to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "kubecon": { "url": "https://kubecon-schedule-mcp.slashben.net/mcp" }
  }
}
```
</details>

<details>
<summary><b>Anything else</b></summary>

Any client with a remote / HTTP MCP option: transport *Streamable HTTP*, URL above, no auth headers.
For stdio-only clients, bridge with `npx mcp-remote https://kubecon-schedule-mcp.slashben.net/mcp`.
</details>

Then ask things like:

- "What's on right now?" or "what starts in the next hour near Grand Ballroom H?"
- "Find Falco or Tetragon talks on Wednesday after 2pm."
- "Which co-located events run on Monday?"
- "Plan my Tuesday around eBPF, runtime security and agent sandboxing, no overlaps."
- "Is Ben Hirschberg speaking, and when?"

## Tools

| Tool | What it does |
| --- | --- |
| `describe_event` | Event name, local time right now, valid days, tracks, rooms, CNCF project tags. Call first. |
| `search_sessions` | AND-combined filters (`query`, `speaker`, `day`, `after`, `before`, `room`, `track`, `project`, `level`), ranked by relevance then time. Breaks hidden unless asked. |
| `get_session` | Full abstract, speakers, room, level, project tags, slides/repo links for one id. |
| `whats_on` | Sessions running at a moment (default: now in event time) and those starting within the next hour. |
| `find_speaker` | Speakers by name or company, with their sessions. |
| `check_agenda` | Order a list of session ids, flag overlaps, unknown ids and venue changes with no walking time. |

Also exposed: an `agenda://day/{day}` resource (one full day, in time order) and a `plan_my_day` prompt.

Output is plain text, one line per session, built to be cheap in tokens:

```
[1236746] | Tue 10 Nov 14:50-15:20 | Hyatt Regency, Level 2, Salt Lake Ballroom DE | Security | Gone in 60 Minutes: ... — Ben Hirschberg, Dr. Constanze Roedig
```

## Run it yourself

```sh
uv sync
uv run kubecon-agenda-mcp            # http://localhost:8080/mcp
uv run pytest
```

Or with Docker:

```sh
docker build -t kubecon-agenda-mcp .
docker run -p 8080:8080 kubecon-agenda-mcp
```

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SESSIONIZE_EVENT_ID` | `svi82w6c` (KubeCon NA 2026) | Sessionize event id from the public `api/v2/<id>/view/All` feed |
| `EVENT_NAME` | KubeCon + CloudNativeCon North America 2026 | Shown to agents |
| `EVENT_SHORT_NAME` | KubeCon NA 2026 | Shown to agents |
| `EVENT_CITY` | Salt Lake City | Shown to agents |
| `EVENT_TIMEZONE` | `America/Denver` | Used for "now" |
| `EVENT_URL` | LF schedule page | Linked from tool output |
| `CACHE_TTL_SECONDS` | `600` | How long the feed is cached in memory |
| `PORT` | `8080` | Listen port |

### Deploy to Google Cloud Run

The server is stateless, so any scale-to-zero container platform works. On Cloud Run:

```sh
gcloud run deploy kubecon-agenda-mcp --source . --region us-central1 \
  --allow-unauthenticated --cpu 1 --memory 512Mi --min-instances 0 --max-instances 5
```

Map a custom domain with `gcloud beta run domain-mappings create` or put it behind a load balancer.

## How it works

Sessionize publishes a public JSON view of the schedule (sessions, speakers, rooms, categories as
separate tables). The server fetches it on demand, joins it into flat session records, caches the
result in memory with ETag revalidation, and keeps serving the last good copy if the feed is down.
Search is a ranked AND over terms: a hit in the title outranks a tag, which outranks the abstract.

No user data is stored. Tool calls are logged as one JSON line each (tool name, filter names used,
result counts) for usage metrics; query text is never logged.

## License

Apache-2.0
