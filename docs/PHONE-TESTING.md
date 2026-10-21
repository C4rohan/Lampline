# 📱 Test a phone agent from your laptop, for free

> **Easier now:** the **Evals** tab does this with Google Voice in your browser, with no speakers or cables, and
> grades each call. See [EVALS.md](EVALS.md). This page is the manual way, with a phone on speaker.

Your laptop plays the caller and your phone carries the call. Lampline's
**Test Caller** template is an AI caller with a persona and a goal. It talks
through your laptop's speakers and listens with its microphone, while your
phone, on speaker, is on a call with the agent you're testing.

```
Test Caller (Lampline, laptop speakers and mic)  ⇄  air or cable  ⇄  your phone on speaker  ⇄  the agent you're testing
```

It costs nothing beyond your phone plan, needs no API access, and works with
any phone agent: one of your Lampline agents on a number, or your own
Dialogflow CX or CX Agent Studio test agent. **Only call agents you own or are
allowed to test, and use made-up details.**

## 1. Make a test caller

1. **Voice agents → New agent → Test Caller**, or run
   `vk templates install test-caller --dir agents/test-caller --name "Test Caller"`.
2. Set its context variables (Agent settings):
   - `persona`: who is calling and how they behave.
   - `goal`: what they want from the call.
   - `caller_name` and `caller_phone`: made-up details they give when asked.
3. Its Start step has **Wait for the other side to speak first** turned on and
   no greeting, so it stays quiet until the agent answers.

## 2. Place the call

1. Dial the agent you're testing from your phone and put it on **speaker**.
2. Lay the phone next to the laptop, about 20–30 cm (8–12 in) from the
   laptop's microphone.
3. On the laptop, open **Test call**, choose the Test Caller, and start the
   call. Do this as the agent starts its greeting.
4. Let the two talk. When the caller's goal is done, or the agent says goodbye,
   the Test Caller hangs up its side. Then end the call on your phone.

The **Calls** page keeps the transcript. In it, the agent you're testing shows
up as the "caller" side, because that's who the Test Caller heard.

## 3. Get clean audio

- **Use the laptop's own speakers (not headphones) in a quiet room.** The
  browser cancels the laptop's own sound from its microphone, but room noise
  and echo still get in.
- **Turn the phone's volume up and the laptop's to medium.** Both sides
  should be clearly louder than the room.
- **Expect some talking over.** The phone's speakerphone ducks one direction
  while the other talks, so an interruption can clip a word. Set the Test
  Caller's turn taking to a slightly longer pause (Integrations → Turn taking)
  if the two keep cutting each other off.
- **Best: a cable instead of the air.** A TRRS splitter (one plug into the
  phone's headset jack or USB-C audio adapter, mic and headphone plugs into
  the laptop) gives clean audio with no echo, still for about $10.

## What you can check

You get what was said, in both directions, and whether the call finished. You
can't see the other agent's steps or tools: for those, test the agent directly
(`vk test --target dfcx|ces` for Google agents). Each test call is manual and
runs one at a time. The **Evals** tab ([EVALS.md](EVALS.md)) keeps each call
with a pass or fail; unattended runs placed through a phone provider are coming
next.
