# Providers and models

Clueless never ships or downloads a language model itself. It uses one that is
already running on your Mac. Every provider URL must be on loopback
(`127.0.0.1`, `localhost` or `::1`); anything else is refused before a
connection is made.

## Supported providers

| Provider | Setting | Default address | Notes |
|---|---|---|---|
| [Ollama](https://ollama.com) | `ollama` | `127.0.0.1:11434` | Easiest. Also provides embeddings |
| llama.cpp `llama-server` | `openai_compat` | `127.0.0.1:8080` | Start it yourself with any GGUF |
| [LM Studio](https://lmstudio.ai) | `openai_compat` | `127.0.0.1:1234` | Enable the local server in LM Studio |
| Jan and other OpenAI-compatible servers | `openai_compat` | set **Server URL** | Must serve `/v1/chat/completions` |
| Managed llama.cpp | `llamacpp` | `127.0.0.1:8081` | Clueless starts `llama-server` for a GGUF file you choose |
| Apple Intelligence | `foundation` | on-device | No download. Less reliable for live answers (see below) |

**Automatic** (the default) probes Ollama, llama.cpp on 8080 and 8081, and LM
Studio, in that order, and uses the first one with a chat model. Configure it in
**Settings → Model**. The walkthrough shows what it found and has a
**Test an answer** button.

## Recommended models

| Memory | Answers | Embeddings |
|---|---|---|
| 16 GB or more | `llama3.1:8b` | `nomic-embed-text` |
| 8 GB | `llama3.2` (3B) | `nomic-embed-text` |

These choices were measured, not assumed. On a battery of grounded meeting
questions, `llama3.1:8b` answered 5 of 5 correctly in about 2 seconds after
prompt tuning. `llama3.2` answered 4 of 5. `qwen2.5` (7B) was faster but
confidently invented a status, which disqualifies it here: a wrong answer
mid-meeting is worse than a slow one.

Embeddings are only needed for Jira, Confluence and code context. Without an
embedding model those sources are disabled; transcript and screen answers still
work.

## Fallbacks

When the configured setup isn't available, Clueless degrades step by step and
tells you which step it is on. The overlay's warning icon and the walkthrough
both show it.

1. **The configured provider and model.** Normal operation.
2. **The model isn't installed, but the provider is up.** Clueless uses the
   largest installed chat model that fits in half of your memory and names the
   substitution. For example: *"llama3.1:8b is not installed; using qwen2.5:14b"*.
   Models too big for memory are skipped, because they thrash swap.
3. **No model server is running.** Clueless uses Apple Intelligence and warns
   that answers are less reliable. In testing, it sometimes declined to answer
   questions the transcript clearly answered.
4. **Nothing works.** The warning lists fixes with copyable commands.

`GET /health` reports the current step under `provider.status` (`ok`,
`degraded` or `none`).

## Offline: importing a GGUF file

On a network that blocks model registries (`ollama pull` failing with
*invalid character '<'* means an HTML block page came back instead of JSON),
copy a model file over from another machine.

**With llama.cpp (simplest):**

1. `brew install llama.cpp`, or copy a `llama-server` binary over.
2. Copy a `.gguf` file over, for example `Llama-3.1-8B-Instruct-Q4_K_M.gguf`.
3. In **Settings → Model**, choose **Run a GGUF file with llama.cpp** and set the
   file path. The `llama-server` path is optional if it's on your `PATH` or in
   Homebrew.
4. Choose **Save & restart**. Clueless starts `llama-server` on
   `127.0.0.1:8081` with embeddings enabled, and stops it when the app quits.
   Its output goes to `logs/llama-server.log`.

**With Ollama:**

```bash
cat > Modelfile <<'EOF'
FROM ./Llama-3.1-8B-Instruct-Q4_K_M.gguf
EOF
ollama create llama3.1:8b -f Modelfile
```

Don't try to route around a network block. Moving a file between machines you
control is the supported path.

## Advanced: remote providers

Clueless refuses non-loopback provider URLs because a remote server would
receive your meeting transcript. If you run a model server on another machine
you control, set `ALLOW_REMOTE_PROVIDER=1` in `.env`. That is deliberately an
environment variable only, with no switch in the settings window. Understand
that the transcript then leaves your Mac.

## Tuning

**Settings → Advanced** (Developer profile) exposes three values:

| Setting | Default | Effect |
|---|---|---|
| Context size | 4096 | KV cache size. The live prompt is about 1,200 tokens, so larger values only cost load time |
| Max answer tokens | 90 | Answers are 1–3 sentences. Raising this was measured to make the model ramble to the cap |
| Temperature | 0.1 | Higher values produced confident answers stitched together from irrelevant context |
