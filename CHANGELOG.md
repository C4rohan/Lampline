# Changelog

Releases are tagged on `prod` ([docs/BRANCHES.md](docs/BRANCHES.md)).

## Unreleased (on dev)

- Response time on the Test call page: how long the caller waits for each reply, split into turn detection, listening, model and speaking; also as `turn_metrics` events.
- Turn taking settings: smart turn (the default, now adjustable) or a fixed pause.
- HD voice (16 kHz) for SIP calls through jambonz, for trunks that carry wideband audio.
- Moonshine: free, streaming speech to text on this computer (English). The text is ready about 0.2–0.3 s after the caller stops, against 0.66 s for the default Whisper.
- CI scans every commit for keys and tokens.
- Pull request template, issue templates, code of conduct and this changelog.
- Engine UI on React 19.3, Radix UI 1.8, React Flow 12.12 and Tailwind CSS 4.3; Dependabot now sends one weekly pull request for the UI's packages.

## v0.1.0

The first release.

- **The engine** on Pipecat: a drag-and-drop builder where LLM steps and scripted pages mix in one agent, text chat, browser calls, and phone calls.
- **Import from Dialogflow CX and CX Agent Studio**: flows, pages, forms, routes, playbooks and tools; and export back to both.
- **20 model providers**: Claude, OpenAI-compatible, Gemini, Vertex AI, Azure OpenAI, Bedrock, Groq, Mistral, DeepSeek, Grok, Together, Fireworks, OpenRouter, Cerebras, NVIDIA NIM, Qwen, SambaNova, Nebius, Novita and Ollama.
- **Phone and SIP**: Twilio, Telnyx, and any SIP trunk, PBX or SBC through jambonz.
- **Data and knowledge** of its own: record collections and local document search.
- **Tools and triggers**: HTTP, MCP and connector tools; schedules, Telegram, WhatsApp and email.
- **`vk`**: lint with a personal-data scan, conversation tests, starter templates and coding-agent skills.
- **One Docker container** for local and on-prem, non-root and key-protected.
- Coming soon: Google Cloud deployment, and Genesys, Avaya, Cisco and other contact centers ([docs/ROADMAP.md](docs/ROADMAP.md)).
