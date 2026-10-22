# 📋 Evals: real phone calls, played and graded

The **Evals** tab calls a voice agent over a real phone line and lets a
**Test Caller** play the caller. You describe who is calling, what they want
and what has to happen; when the call ends, your model grades the transcript
against the goal and each criterion. Every run is kept with its transcript and
result.

It works with any phone agent: one of your Lampline agents on a number, or your
own Dialogflow CX or CX Agent Studio test agent. **Only call agents you own or
are allowed to test, and use made-up details.**

```
Test Caller (Lampline)  ⇄  bridge  ⇄  Google Voice in your browser  ⇄  phone network  ⇄  the agent you're testing
```

## How the call goes out

| | Google Voice (free) | Phone on speaker |
|---|---|---|
| What you need | A Google Voice number, Chrome or Edge, and the **Lampline Phone Bridge** extension | Any phone, or a phone app plus a virtual audio cable |
| Audio | Digital, inside the browser: no speakers, microphone or cables | Through the air, or a virtual cable |
| What you do | Press **Call** in the Google Voice tab Lampline opens | Dial from your phone on speaker |
| Windows / Mac | Both | Both |

A third way, Lampline placing the call itself through Twilio for unattended
runs, is coming next.

## Set up Google Voice (once)

1. Get a Google Voice number at [voice.google.com](https://voice.google.com)
   (free for personal use in the US; calls to US and Canadian numbers are free).
2. Install the extension: open `chrome://extensions` (Edge:
   `edge://extensions`), turn on **Developer mode**, choose **Load unpacked**
   and pick the `extension` folder of your Lampline download.
3. Open Lampline's **Evals** tab. It shows "Lampline Phone Bridge … is
   installed".

## Run an eval

1. **Caller:** the Test Caller (press **Create Test Caller** if you don't have
   one). It waits for the other side to speak first.
2. **Number to call:** the agent you're testing, like `+15550101234`.
3. **How the call goes out:** Google Voice or phone on speaker.
4. **Who is calling** and **what they want:** the Test Caller plays this person.
5. **Pass criteria:** one per line, for example "The agent asks for the
   caller's name" or "The agent confirms the new time before hanging up".
6. Press **Start eval**.
   - **Google Voice:** a Google Voice tab opens with the number filled in.
     Press **Call**. The Test Caller talks; you watch the conversation on the
     Evals page. When the Test Caller says goodbye, the bridge ends the Google
     Voice call too (or hang up yourself).
   - **Phone on speaker:** dial the agent from your phone on speaker next to
     the computer. Pick the microphone and speaker (or your virtual audio
     cable) on the page.
7. The run shows **Passed** or **Failed**, with the reason for each criterion.
   A call where the Test Caller heard nothing shows **Didn't run** instead, so
   a bad connection never counts as a failed agent.

## Make the caller tougher

Real callers aren't polite test scripts. Open **Make the caller tougher** on
the Evals page to make the Test Caller harder to handle:

| Option | What it does |
|---|---|
| Background noise | A busy room, a street or a car, mixed under the caller's voice at a low, medium or high level (about 25, 15 or 8 dB below the voice) |
| Caller's voice | Another voice of your text-to-speech service, for an accent or a different-sounding caller: a Kokoro voice like `bf_emma` (British), a Deepgram voice, or a Fish Audio voice id |
| How the caller talks | Hesitant (ums and restarts), terse, rambling, or English as a second language |
| Cuts in on the agent | Sometimes or often, the caller starts talking 1 to 2.5 seconds into the agent's reply ("Sorry to cut in, but…"). Never on the greeting |
| Long pauses | Once or several times, the caller goes quiet for 6 to 10 seconds before answering. Does the agent check in, wait, or hang up? |

Cutting in and pauses are planned from a seed, which the run keeps, so a
replay plays them the same way.

**The agent's response time, from the caller's side.** Every eval (tough or not)
times the agent the way a caller experiences it: from the moment the Test
Caller stops speaking to the moment it hears the agent start, phone network
and all. The result shows the typical (p50), slow (p95) and slowest response.
For your own Lampline agents, the Calls page shows the same measured inside
the engine, so the difference is what the phone line adds.

## Replay a call

- **A failed eval:** press **Run again** for the same eval (same caller options
  and seed), or **Replay what the caller said** to have the Test Caller say the
  same lines, in order. Its own cut-ins aren't replayed as lines; the seed brings
  them back.
- **A real call** to one of your Lampline agents: on the Calls page, press
  **Replay as a test**. The Evals page opens with the caller's lines, and the
  agent's call checks as the pass criteria. Add the number to call and start.

## Import test scripts

Already have IVR or voice tests in **Hammer**, **Cyara**, a spreadsheet or a
document? Bring them in instead of typing each one.

1. In **Evals → Test cases**, press **Import test scripts**.
2. Paste the script, or choose a file exported as text, CSV, XML or JSON (up to
   120,000 characters at a time; split bigger exports).
3. Press **Convert to test cases**. Your model reads each test and writes a
   case: who the caller is and what to say at each point, what they want, and
   the checks (from the expected prompts and results). Nothing is saved yet.
4. Untick any you don't want, and press **Save**.
5. Press **Use** on a case to fill in the eval, check the number, and **Start
   eval**. The run is linked to its case.

Good to know:
- **Keypad steps (DTMF)** become spoken digits: the Test Caller says "4 4 7 2 1"
  where the script pressed keys. That works for menus that accept speech; a
  case shows a note when the script relies on keys or on things a caller
  can't see (a callback, a screen pop, a database).
- Expected prompts become checks in plain words ("The system asks for the
  account number"), graded against what was actually said, so a reworded
  recording still passes.
- The script goes to your model like any other request. Use made-up test data,
  never real customers' details.

## What the extension does, and doesn't

- It only acts in a Google Voice tab that Lampline itself opened for an eval.
  Your other Google Voice tabs are untouched.
- In that tab it gives Google Voice the Test Caller's voice as the
  microphone, and sends the call's incoming audio to Lampline on this computer
  (`localhost`). Your real microphone isn't used.
- It doesn't press **Call** for you: Google Voice's policy doesn't allow
  automating it to place calls, so you place each call yourself. Use evals for
  testing your own agents, not for calling people.
- It talks to Lampline only at `http://localhost` and `http://127.0.0.1`, with
  a token that starts one call. Your API key never reaches it.
- The source is the `extension` folder: four short files you can read.

## Tips

- One eval at a time per Google Voice account.
- A slow first answer? Turn-taking settings for the Test Caller are in
  **Integrations → Turn taking**.
- Runs keep the number called and what was said. Like call history, they stay
  on this computer (or behind `VPE_REMOTE_EDITING`).

## For developers

- API: `POST /v1/evals` takes `caller` for a tougher caller: `{noise: room|street|car, level: low|medium|high,
  voice, style: hesitant|terse|rambling|non_native, interrupt: sometimes|often, pauses: once|several, seed}`.
  A run shows `options` and `agent_ms` (`{turns, p50, p95, max}`, from the caller's side).
  `POST /v1/evals/replay` (`{run_id, exact}` or `{call_id}`) returns a draft eval and saves nothing.
  `GET /v1/evals/cases`, `POST /v1/evals/cases` (save `{cases, source}`),
  `POST /v1/evals/cases/convert` (`{text, filename}` → cases, nothing saved),
  `DELETE /v1/evals/cases/{id}`, `POST /v1/evals`, `GET /v1/evals`, `GET /v1/evals/{id}`,
  `POST /v1/evals/{id}/offer` (phone on speaker), `POST /v1/evals/{id}/stop`,
  `DELETE /v1/evals/{id}`, and `POST /v1/evals/bridge/offer` (the extension,
  with its one-call token in `X-Lampline-Bridge`). See
  [engine/README.md](../engine/README.md#api).
- Engine tests: `engine/tests/test_evals.py` (a real WebRTC call from aiortc
  through the bridge route, then grading).
- End-to-end test of the real extension in Chromium, with a stand-in for
  Google Voice and the far end of the phone line:
  `LAMPLINE_PYTHON=<engine venv>/bin/python node engine/tests/e2e/bridge.e2e.mjs`
  (needs Playwright and Chromium). It checks that the call's audio reaches the
  Test Caller, that the Test Caller's voice reaches the far end, and that
  hanging up ends and grades the run.
