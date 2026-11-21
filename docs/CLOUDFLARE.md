# Running the AI features on Cloudflare's free tier

Print Handy's AI features are optional, and every one of them talks to the one endpoint you configure in `[llm]`. This guide shows how to point that endpoint at Cloudflare: [Workers AI](https://developers.cloudflare.com/workers-ai/) serves the models through its OpenAI-compatible endpoint, no Worker of your own needed. A free Cloudflare account covers light home use.

> **Verification status.** Nothing in this guide has been run against a real Cloudflare account by the project: no account was available. The settings are the ones Print Handy documents in [SETTINGS.md](SETTINGS.md). Cloudflare facts (model names, prices, limits) were checked against Cloudflare's documentation on **2026-10-29**, with links next to each one; they change, so recheck them before you rely on them.

| Feature | What you set | Cloudflare piece |
| --- | --- | --- |
| AI design and optional Web UI chat | `llm.chat_url`, `llm.model`, `LLM_API_KEY` | Workers AI's OpenAI-compatible endpoint |
| Camera watchdog, spool label and purchase reads | the same endpoint, with a vision-capable model name (see below), and `vision.ai_enabled: true` | Workers AI's OpenAI-compatible endpoint |

You can use the chat and design features without the camera checks, and the other way around.

## Before you start

1. A Cloudflare account. The Workers Free plan includes the Workers AI daily free allocation ([pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)).
2. Your **account ID** and a **Workers AI API token**. In the dashboard, open the Workers AI page, select **Use REST API**, then **Create a Workers AI API Token**, and copy the account ID shown there ([Cloudflare's steps](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)). A token you build by hand needs both `Workers AI - Read` and `Workers AI - Edit`.

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
- **Design uses the same endpoint.** `/print-design` posts to `llm.chat_url` with the same key; `design.model` overrides only the model name when set ([SETTINGS.md](SETTINGS.md)). A common split is the 8B model for chat and `design.model: "@cf/meta/llama-3.3-70b-instruct-fp8-fast"` for design. Design is on automatically once an endpoint is configured; `design.enabled: false` turns it off.
- **Chat is off by default.** Design uses the endpoint as soon as it is configured. Web UI chat uses the model only when `webui.chat_llm_enabled` is `true`. Discord, Telegram and Matrix never send a message to a model.
- **Timeouts.** `llm.timeout_s` defaults to 20 seconds and `design.timeout_s` to 180. Raise `llm.timeout_s` if chat replies time out on the 70B model.
- **Roasts need no endpoint.** Roasts come from the local roast corpus and never call a model, so nothing here affects them.

## 2. Camera watchdog through the same endpoint

AI camera checks are off by default: set `vision.ai_enabled: true` (or `PRINT_HANDY__VISION__AI_ENABLED=true`) as well as the endpoint, or no frame is ever sent. The checks post the frame to `llm.chat_url` like every other call, so the model answering them must accept images: Workers AI's [`@cf/meta/llama-3.2-11b-vision-instruct`](https://developers.cloudflare.com/workers-ai/models/llama-3.2-11b-vision-instruct/) does. Name it as the small tier's model so the text features keep their own model:

```yaml
ai:
  small:
    model: "@cf/meta/llama-3.2-11b-vision-instruct"
```

Accept Meta's license once for your account first: Cloudflare's model page says to send one request to the model with `"prompt": "agree"`. Anything the model cannot read becomes `uncertain`, which alerts; it never turns a failure into `ok`. Spool label photos and purchase reads use the same route. If you would rather keep frames at home, the [local failure detector](SETTINGS.md#local-failure-detector) runs on your own machine instead.

## 3. Free tier limits and rough usage

Checked on 2026-10-29:

- **Workers AI:** 10,000 Neurons per day at no charge, on both Workers Free and Workers Paid, reset daily at 00:00 UTC. Workers Free has no paid overage (the pricing table lists none), so expect AI requests past the allocation to fail until the reset; Workers Paid bills $0.011 per 1,000 Neurons ([pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)).

Neuron rates from the pricing page, per million tokens:

| Model | Input | Output |
| --- | --- | --- |
| `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | 26,668 | 204,805 |
| `@cf/meta/llama-3.1-8b-instruct-fp8-fast` | 4,119 | 34,868 |
| `@cf/meta/llama-3.2-11b-vision-instruct` | 4,410 | 61,493 |

Rough per-feature estimates. They are back-of-the-envelope figures from Print Handy's request sizes, not measurements:

| Feature | One use | Uses per day within 10,000 Neurons (that feature alone) |
| --- | --- | --- |
| Chat message, 70B model (about 1,500 tokens in, up to 200 out) | about 80 Neurons | about 120 |
| Chat message, 8B model | about 13 Neurons | about 750 |
| Design turn, 70B model (a few thousand tokens in, up to 4,000 out; each automatic render repair is another turn) | about 400 to 900 Neurons | about 10 to 25 turns |
| Camera check (one frame) | about 10 to 35 Neurons; Cloudflare does not publish how many tokens an image counts as | about 300 to 1,000 checks; at the default sensitivity (a check every 15 minutes plus two at the start) a 10-hour print is about 42 checks |

Design is the feature that can spend the day's allocation. When the allocation runs out, chat and design fail soft (the print flow carries on) and camera checks answer `uncertain` with the error in the alert text. The [Workers AI dashboard](https://dash.cloudflare.com/?to=/:account/ai/workers-ai) shows actual use.

## 4. Privacy: what leaves the house

Cloudflare's [Workers AI data usage](https://developers.cloudflare.com/workers-ai/platform/data-usage/) page says inference inputs and outputs are not retained after the request for standard inference and are not used to train models. Per feature, this is what is sent:

- **Chat:** the message you typed in Web Chat and Print Handy's own system prompt (its command list). Nothing from the printer is sent unless it is in your message.
- **Design:** your part description and follow-up requests, the OpenSCAD code from earlier turns, and render errors.
- **Camera watchdog:** camera frames (JPEG) from your printer, at every check, plus the watchdog's fixed question. This is the most personal data on the list: the frame shows whatever the camera sees.

Keep the API token in `.env` and out of screenshots and bug reports: anyone with it can spend your allocation.
