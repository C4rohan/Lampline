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

- API: `POST /v1/evals`, `GET /v1/evals`, `GET /v1/evals/{id}`,
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
