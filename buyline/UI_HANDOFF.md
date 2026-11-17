# Buyline: new product UI + supplier sourcing (handoff to the build agent)

## What this is

`app.html` is a full replacement for `buyline/index.html`. Drop it into `buyline/` (it is a single file, no build step) and open `http://localhost:8020/app.html` with `serve.py` running. It works today against the current `state.json`, `POST /start`, `POST /approve`, and `POST /not-now`. Without a `state.json` (or with `?preview`), it plays a clearly labelled simulated run that shows the full target experience.

## The product the UI presents

The user uploads what they want to buy. The **Buyline agent** thinks, Fo acts, and the user decides. The UI makes the three actors visible everywhere: violet = Buyline agent, teal = Fo, amber = You.

- **New request page:** drop an invoice, price list, spec sheet or email, or type the need. Fields for quantity, deadline, budget, target price and ship-to. **Who should we ask?** offers three modes: *Let Buyline find suppliers* / *Only my suppliers* / *Both*. **How many to contact** is *Auto* (the default) or 3 / 5 / 8 / 12. There is a supplier book with checkboxes, an "add a supplier" row, and ground rules: negotiate (rounds; walk-away price stays private), always ask me before ordering, pay with Fo's single-use card after I confirm.
- **Request page:**
  - The header shows the best deal so far.
  - An 8-stage rail: Request, Brief, Sourcing, Outreach, Negotiate, Your decision, Order, Track. Each stage shows who owns it and what it produced (for example "12 found → 4 picked").
  - **Live** tab:
    - **What's happening** is a timeline of agent thoughts (reasoning in plain language), Fo actions (live call status, live transcript lines, the quote card with trap flags) and the user's decisions.
    - The **board** on the right shows the best deal (savings vs. first quote, vs. budget, trap avoided), suppliers in play with live status, and the market check bar (low / typical / high, our deal, budget per unit).
  - **Brief** tab: the purchase brief Fo works from, with a Download .md button.
  - **Suppliers** tab: every candidate found, with its source, location, fit score and why it was contacted or skipped.
  - **Quotes** tab: ranked by landed cost, with the reason each one was ruled out.
  - **Calls & emails** tab: every instruction we gave Fo and every report Fo sent back.
  - **Order** tab: the PO Fo emailed, and a tracker for PO emailed → payment confirmed → shipped → delivered.
- **Decision sheet** (opens automatically at `approve`): option cards with the recommendation preselected. The user can pick any other viable option; ruled-out options are disabled with the reason. It also shows the recommendation's reasons, the PO text, and the payment row (Fo single-use card, confirm by text). Buttons: Approve and send PO / Not now.

## Backend changes so the live mode shows everything (in priority order)

1. **Supplier sourcing (the big gap: today it is always the same 3).** Add `sourcing` to `state.json` at the `suppliers` stage:
   ```json
   "sourcing": {
     "mode": "ai" | "mine" | "both",
     "target": "auto" | 3 | 5 | 8 | 12,
     "reason": "Found 12 candidates ... Auto picked 4 because ...",
     "found": [
       {"id": "A", "name": "...", "source": "ai" | "yours" | "added", "location": "Oakland, CA", "distance": "6 mi",
        "stock": "12AWG THHN in stock", "channels": ["phone", "email"], "score": 88,
        "status": "shortlisted" | "skipped", "why": "one line: why picked or skipped"}
     ]
   }
   ```
   - Discovery: reuse `market.py`'s Claude + web_search pattern to return candidate suppliers (name, location, phone, email, what they stock), then score them in code on stock match, minimum-order fit, distance or lead time, and reachability (has a phone or email).
   - Auto count: about 3 for small orders, 4–5 for mid-size, up to 8 when candidates are many and the order is large. Never more than the number of good candidates.
   - **Safety stays:** candidates found on the web are shown, but in dry/live every call and email still goes only to `FO_TEST_PHONE_*` and `FO_TEST_EMAIL`. Say so on screen (the mode pill already does).
   - "Only my suppliers" uses a `suppliers.json` supplier book, plus any added in the form.
2. **Read the new form fields.** `/start` currently gets one request string (the UI appends "Sourcing: find suppliers, contact as many as needed."). Better: accept `{request, sourcing_mode, target_count, my_suppliers: [...], rounds, image}` as JSON fields and pass them to `run()`.
3. **Approve a non-recommended pick.** The UI posts `{supplier: "<id>"}` to `/approve`. Honour it: send the PO to that supplier, if it is viable.
4. **`events` (optional, best storytelling).** Append `{at, actor: "agent"|"fo"|"you", kind: "think"|"act"|"result"|"ask", title, text, chips: [[label, value, "good"|"bad"|"warn"]], call: <call object>}` as the run goes. Fill the agent's "think" events with the real reasoning (why B is out, the round plan, why it stopped). Without `events`, the UI derives the timeline from calls, quotes, rounds and the recommendation, which works but reads flatter.
5. **Call status values the UI understands:** `queued`, `dialing`, `on the call`, `no answer`, `sending` (email, with `channel: "email"`), `quote in`, `counter in`. For a live transcript during a call, append `lines: [["Fo", "..."], ["Supplier", "..."]]` to the event.
6. **Payment (later).** After the PO, have Fo request payment with its single-use card; Fo texts the owner to confirm. Write `payment: "requested" | "confirmed"` to state. The UI's order tracker already reads `payment`.

## Do not

- Do not let discovery dial or email real businesses. Display only.
- Do not show the walk-away (hard line) price anywhere in the UI or in Fo's prompt. The UI shows only the open and best-and-final prices.
