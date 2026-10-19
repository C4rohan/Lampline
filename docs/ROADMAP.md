# Roadmap

What Lampline does today, and what's coming. Want something sooner?
[Open an issue](https://github.com/C4rohan/lampline/issues) or a pull request
against `dev` ([how](BRANCHES.md)).

## Where it runs

| Target | Status | What you get |
|---|---|---|
| Local and on-prem | **Available** | One Docker container (`scripts/docker-up.sh`) or the engine natively (`scripts/engine-up.sh`) on a laptop, a workstation or any server you control. Calls, agents, records and knowledge stay on your hardware. |
| Google Cloud | Coming soon | A one-command deployment of the same container to your own Google Cloud project, with secrets in Secret Manager, a private data volume and an HTTPS address for phone providers. |
| AWS, Azure | Later | The same, when people ask for them. |

## Models

Available now, each through Pipecat's own service for it: Claude (Anthropic),
any OpenAI-compatible endpoint, Gemini (Google AI Studio), Gemini on Vertex AI,
Azure OpenAI, Amazon Bedrock, Groq, Mistral, DeepSeek, Grok (xAI), Together AI,
Fireworks AI, OpenRouter, Cerebras, NVIDIA NIM, Qwen, SambaNova, Nebius,
Novita AI and Ollama. Pick one on the Integrations page.

## Phone and SIP

| Connection | Status |
|---|---|
| Twilio numbers | **Available** (inbound and outbound) |
| Telnyx numbers | **Available** (inbound and outbound) |
| SIP through jambonz: any SIP trunk, PBX or session border controller | **Available** (inbound) |
| SIP through jambonz: outbound calls | Coming soon |

## Contact centers (coming soon)

Hand calls between your agents and the platform your team already uses. Until
each one lands, most of them can already send calls to your agents over a SIP
trunk through jambonz.

| Platform | Planned approach |
|---|---|
| Genesys Cloud | AudioHook streams the call to your agent; transfers go back to an Architect flow or queue |
| Avaya (Experience Platform, Aura) | SIP through jambonz, with transfers back to skills and VDNs |
| Cisco Webex Contact Center | A virtual agent connector for Webex Contact Center flows, with escalation to live agents |
| Cisco UCCE and PCCE | Calls from CVP over SIP through jambonz, with transfers back to the call flow |
| Amazon Connect | Calls routed from a contact flow to your agent, and back to a queue when a person is needed |
| NICE CXone | A CXone virtual agent integration, with transfers back to skills |
| Five9 | Calls from Five9 IVA scripts over SIP through jambonz, with transfers back to skills |
| Twilio Flex | Twilio numbers already work; this adds transfers into Flex TaskRouter queues with the call's details |

## Done

- Agents as code with `vk`: lint with a personal-data scan, conversation tests, starter templates, and skills for coding agents
- The engine on Pipecat: text chat, browser calls, phone calls, auto-reload
- Drag-and-drop builder with a root agent, sub-agents, transfers and tools; describe an agent in words to draft it
- Scripted pages and flows mixed with LLM steps: exact wording, forms, keypad input, rule transfers
- Dialogflow CX and CX Agent Studio import and export, both ways
- HTTP, MCP and connector tools; triggers, Telegram, WhatsApp and email
- Data and knowledge of its own: record collections and local RAG (keywords plus on-device embeddings)
- One Docker container, non-root, key-protected
- 20 model providers; SIP through jambonz

## Next

- Google Cloud deployment
- Genesys Cloud AudioHook, then the other contact centers above
- Live-agent transfers and call recordings
- Published benchmarks: latency, quality and calls per GPU for common speech and model stacks
- LLM-played callers for tests, and an MCP server for `vk`
