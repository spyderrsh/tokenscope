# tokenscope

Explore your Claude Code token usage, estimated cost, and prompt-cache misses.

tokenscope reads the transcripts that Claude Code keeps in `~/.claude/projects`. It does not use the network and it sends nothing anywhere.

> This is an unofficial tool. It is not made or supported by Anthropic.

## Install

1. Install [uv](https://docs.astral.sh/uv/): `brew install uv`.
2. Install tokenscope:

   ```sh
   uv tool install git+https://github.com/spyderrsh/tokenscope
   ```

   If your network intercepts TLS (a corporate proxy such as Zscaler), add `--system-certs` so that uv trusts the certificates in your system keychain:

   ```sh
   uv tool install --system-certs git+https://github.com/spyderrsh/tokenscope
   ```

3. If `tokenscope` is not found, run `uv tool update-shell` and open a new terminal.

To update, run `uv tool upgrade tokenscope`. To remove it, run `uv tool uninstall tokenscope`.

## Use

```sh
tokenscope            # last 30 minutes
tokenscope 1440       # last 24 hours
tokenscope --print    # plain-text report, for pipes and logs
tokenscope --print --all-sessions
```

On a terminal, tokenscope opens an interactive explorer:

| Screen | What it shows | Keys |
|---|---|---|
| Sessions | One row per session: last active, title, model, calls, subagents, context size, cache hit %, estimated cost, cache misses | `Enter` open · `s` sort · `/` filter · `w` change window · `r` reload · `?` column key · `q` quit |
| Session | Each API call of the session in time order, for the main thread and each subagent | `Enter` call detail · `m` misses only · `Esc` back |
| Call detail | Tokens and cost for each part, the previous call, and why the cache missed | `Esc` close |

When stdout is not a terminal, tokenscope prints the text report.

## Columns

| Column | Meaning |
|---|---|
| ctx | Tokens sent in one call: input + cache write + cache read |
| hit% | Share of ctx that was read from the prompt cache |
| cache wr / cache rd | Tokens written to / read from the prompt cache |
| misses | Calls after the first in a transcript that wrote more cache than they read |
| idle 2h | A cache miss after a 2-hour gap: the cache entry had expired |
| from X | A cache miss after a switch from model X: each model has its own cache |
| ≈$ | Estimated cost at API list prices. It is not a subscription bill. |

Every call reads the full cached prompt again, so the sum of cache reads is approximately calls × ctx avg. Large cache-read totals are normal.

## Prices

The cost estimate uses the price table at the top of `src/tokenscope/cli.py` (list prices as of 2026-09-25). Cache writes cost 1.25× the input price for the 5-minute cache and 2× for the 1-hour cache. A model that is not in the table shows `?`.

## License

MIT
