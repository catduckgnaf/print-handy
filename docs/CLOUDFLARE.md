# Running the AI features on Cloudflare's free tier

Print Handy's AI features are optional, and each one talks to an endpoint you choose. This guide shows how to point all of them at Cloudflare: [Workers AI](https://developers.cloudflare.com/workers-ai/) for the models and two small reference Workers in this repository for the camera and lookup contracts. A free Cloudflare account covers light home use.

> **Verification status.** Nothing in this guide has been run against a real Cloudflare account by the project: no account was available. The two Workers are tested offline against a mock AI binding (`node --test`, see below), and the settings are the ones Print Handy documents in [SETTINGS.md](SETTINGS.md). Cloudflare facts (model names, prices, limits) were checked against Cloudflare's documentation on **2026-10-29**, with links next to each one; they change, so recheck them before you rely on them.

| Feature | What you set | Cloudflare piece |
| --- | --- | --- |
| AI design, and chat in plain language once `discord.chat_llm_enabled` is `true` | `llm.chat_url`, `llm.model`, `LLM_API_KEY` | Workers AI's OpenAI-compatible endpoint, no Worker needed |
| Camera watchdog | `PRINT_VISION_URL` and `vision.ai_enabled: true` | [`examples/cloudflare/vision-verdict`](../examples/cloudflare/vision-verdict/README.md) Worker |
| Lookup by meaning in `/print-lookup` | `PRINT_FUZZY_URL` | [`examples/cloudflare/fuzzy-lookup`](../examples/cloudflare/fuzzy-lookup/README.md) Worker |

You can use any one of these without the others.

## Before you start

1. A Cloudflare account. The Workers Free plan includes the Workers AI daily free allocation ([pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)).
2. Your **account ID** and a **Workers AI API token**. In the dashboard, open the Workers AI page, select **Use REST API**, then **Create a Workers AI API Token**, and copy the account ID shown there ([Cloudflare's steps](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)). A token you build by hand needs both `Workers AI - Read` and `Workers AI - Edit`.
3. For the two Workers only: Node.js for `npx wrangler` ([supported versions](https://developers.cloudflare.com/workers/wrangler/install-and-update/)).

## 1. Chat and design through Workers AI

Workers AI has an OpenAI-compatible chat completions endpoint ([docs](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/)), which is the shape Print Handy's `llm.*` settings expect. It takes the API token as a bearer token, which Print Handy sends from the variable `llm.api_key_env` names.

In your YAML settings file:

```yaml
llm:
  chat_url: https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1/chat/completions
  model: "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
  api_key_env: LLM_API_KEY   # the default; shown for clarity
```

or as environment variables (Docker `.env`, Compose, Unraid template):

```sh
PRINT_HANDY__LLM__CHAT_URL=https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1/chat/completions
PRINT_HANDY__LLM__MODEL=@cf/meta/llama-3.3-70b-instruct-fp8-fast
LLM_API_KEY=<your Workers AI API token>
```

Keep the token in the variable (`LLM_API_KEY`, or whatever `llm.api_key_env` names), never in the settings file.

- **Model choice.** [`@cf/meta/llama-3.3-70b-instruct-fp8-fast`](https://developers.cloudflare.com/workers-ai/models/llama-3.3-70b-instruct-fp8-fast/) is a large, fast Llama model and the better choice for design. `@cf/meta/llama-3.1-8b-instruct-fp8-fast` (listed on the [pricing page](https://developers.cloudflare.com/workers-ai/platform/pricing/)) costs about a sixth as much per token and is enough for turning chat into commands. The [model catalog](https://developers.cloudflare.com/workers-ai/models/) lists the rest.
- **Design inherits.** `/print-design` uses `llm.chat_url`, `llm.model` and `llm.api_key_env` unless `design.chat_url`, `design.model` or `design.api_key_env` are set ([SETTINGS.md](SETTINGS.md)). A common split is the 8B model for chat and `design.model: "@cf/meta/llama-3.3-70b-instruct-fp8-fast"` for design. Design is on automatically once an endpoint is configured; `design.enabled: false` turns it off.
- **Chat is off by default.** Design uses the endpoint as soon as it is configured. Chat in plain language is a Discord (and web chat) feature that stays off until you set `discord.chat_llm_enabled: true` (`webui.chat_llm_enabled: true` for web chat).
- **Timeouts.** `llm.timeout_s` defaults to 20 seconds and `design.timeout_s` to 180. Raise `llm.timeout_s` if chat replies time out on the 70B model.
- **Roasts need no endpoint.** Roasts come from the local roast corpus and never call a model, so nothing here affects them.

## 2. Camera watchdog through the vision-verdict Worker

AI camera checks are off by default: set `vision.ai_enabled: true` (or `PRINT_HANDY__VISION__AI_ENABLED=true`) as well as `PRINT_VISION_URL`, or the Worker is never called. The watchdog's `PRINT_VISION_URL` contract (one POST with a JPEG and a question, answered with a verdict; see [Custom vision endpoint](SETTINGS.md#custom-vision-endpoint-print_vision_url)) needs something that turns a model's free text into Print Handy's verdicts. The [vision-verdict Worker](../examples/cloudflare/vision-verdict/README.md) does that with Workers AI's [`@cf/meta/llama-3.2-11b-vision-instruct`](https://developers.cloudflare.com/workers-ai/models/llama-3.2-11b-vision-instruct/).

1. Accept Meta's license once for your account: Cloudflare's model page says to send one request to the model with `"prompt": "agree"`. The exact `curl` is in the [Worker's README](../examples/cloudflare/vision-verdict/README.md#deploy).
2. Deploy:

   ```sh
   cd examples/cloudflare/vision-verdict
   npx wrangler login
   npx wrangler secret put VISION_SECRET
   npx wrangler deploy
   ```

3. Set `PRINT_VISION_URL=https://print-handy-vision-verdict.<your-subdomain>.workers.dev/<the secret>` in Print Handy's environment and restart.

`PRINT_VISION_URL` takes precedence over `services.vision_chat_url` and needs no key: the secret is the URL path, because Print Handy sends no `Authorization` header. Anything the Worker cannot read becomes `uncertain`, which alerts; it never turns a failure into `ok`. The Worker answers verdicts only, so spool-label photo reads sent to the same URL fall back to the manual steps. If you would rather keep frames at home, the [local failure detector](SETTINGS.md#local-failure-detector) runs on your own machine instead.

## 3. Lookup by meaning through the fuzzy-lookup Worker

`/print-lookup` matches locally first. Only when nothing matches locally does Print Handy offer the query to `PRINT_FUZZY_URL` (see [Semantic lookup endpoint](SETTINGS.md#semantic-lookup-endpoint-print_fuzzy_url)). The [fuzzy-lookup Worker](../examples/cloudflare/fuzzy-lookup/README.md) answers that contract with Workers AI's [`@cf/baai/bge-small-en-v1.5`](https://developers.cloudflare.com/workers-ai/models/bge-small-en-v1.5/) embeddings and cosine similarity. It is stateless (no KV, D1 or Vectorize), skips the model on an exact match, caches embeddings in memory, and degrades to `{"matches": [], "degraded": true}` on a quota or model error, which leaves the local answer in place.

```sh
cd examples/cloudflare/fuzzy-lookup
npx wrangler login
npx wrangler secret put LOOKUP_SECRET
npx wrangler deploy
```

Then set `PRINT_FUZZY_URL=https://print-handy-fuzzy-lookup.<your-subdomain>.workers.dev/<the secret>` and restart.

## 4. Free tier limits and rough usage

Checked on 2026-10-29:

- **Workers AI:** 10,000 Neurons per day at no charge, on both Workers Free and Workers Paid, reset daily at 00:00 UTC. Workers Free has no paid overage (the pricing table lists none), so expect AI requests past the allocation to fail until the reset; Workers Paid bills $0.011 per 1,000 Neurons ([pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)).
- **Workers (for the two Workers):** 100,000 requests per day on Workers Free, 10 ms CPU time per request (the page says waiting on network requests does not count as CPU time), 50 subrequests per invocation ([limits](https://developers.cloudflare.com/workers/platform/limits/)). The requests from one home install are far below this.

Neuron rates from the pricing page, per million tokens:

| Model | Input | Output |
| --- | --- | --- |
| `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | 26,668 | 204,805 |
| `@cf/meta/llama-3.1-8b-instruct-fp8-fast` | 4,119 | 34,868 |
| `@cf/meta/llama-3.2-11b-vision-instruct` | 4,410 | 61,493 |
| `@cf/baai/bge-small-en-v1.5` | 1,841 | none |

Rough per-feature estimates. They are back-of-the-envelope figures from Print Handy's request sizes, not measurements:

| Feature | One use | Uses per day within 10,000 Neurons (that feature alone) |
| --- | --- | --- |
| Chat message, 70B model (about 1,500 tokens in, up to 200 out) | about 80 Neurons | about 120 |
| Chat message, 8B model | about 13 Neurons | about 750 |
| Design turn, 70B model (a few thousand tokens in, up to 4,000 out; each automatic render repair is another turn) | about 400 to 900 Neurons | about 10 to 25 turns |
| Camera check (one frame) | about 10 to 35 Neurons; Cloudflare does not publish how many tokens an image counts as | about 300 to 1,000 checks; at the default sensitivity (a check every 15 minutes plus two at the start) a 10-hour print is about 42 checks |
| Lookup by meaning, cold cache (about 120 candidates, 600 tokens) | about 1 Neuron | thousands |
| Lookup by meaning, warm cache | a small fraction of a Neuron | effectively unlimited |

Design is the feature that can spend the day's allocation. When the allocation runs out, chat and design fail soft (the print flow carries on), camera checks answer `uncertain` with the error in the alert text, and lookups keep their local answer. The [Workers AI dashboard](https://dash.cloudflare.com/?to=/:account/ai/workers-ai) shows actual use.

## 5. Privacy: what leaves the house

Cloudflare's [Workers AI data usage](https://developers.cloudflare.com/workers-ai/platform/data-usage/) page says inference inputs and outputs are not retained after the request for standard inference and are not used to train models. Per feature, this is what is sent:

- **Chat:** the message you typed in the print channel and Print Handy's own system prompt (its command list). Nothing from the printer is sent unless it is in your message.
- **Design:** your part description and follow-up requests, the OpenSCAD code from earlier turns, and render errors.
- **Camera watchdog:** camera frames (JPEG) from your printer, at every check, plus the watchdog's fixed question. These go to your Worker and from it to Workers AI. This is the most personal data on the list: the frame shows whatever the camera sees.
- **Lookup by meaning:** the `/print-lookup` text you typed, only when nothing matched locally, plus Print Handy's public registry entries (ids and display names). Print history, print numbers and spool IDs are never sent.

The reference Workers write nothing to storage and log nothing. If you turn on Workers Logs or another logging feature in the dashboard, Cloudflare's retention rules for that feature apply. The secret URLs act as passwords: anyone with one can spend your allocation, so keep them in `.env` and out of screenshots and bug reports.

## Testing the Workers offline

From the repository root, with Node.js installed and nothing else:

```sh
node --test "examples/cloudflare/**/*.test.mjs"
```

`tests/test_cloudflare_examples.py` runs the same tests as part of `pytest` when `node` is on PATH and skips them otherwise. The project's offline CI image does not ship Node, so CI checks the Workers' limits against Print Handy's client but skips the Node tests.
