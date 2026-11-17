# Buyline

**A procurement agent that researches the market, calls your suppliers, haggles, and has the order ready for you to approve by morning.**

Built at Agents After Dark (Berkeley). Buyline is the brain; Fo, a phone and email agent, is the hands.

## The problem

Small businesses lose hours phoning suppliers for quotes, one call at a time, during business hours. A cheap unit price can hide a trap: a minimum order bigger than you need, shipping, fees or duty. Nobody has time to haggle, so buyers pay the first price.

## What Buyline does

1. **You say what you need.** Type a request ("500 m of 12 AWG copper wire, within 14 days, budget $425") or drop in an old invoice, price list or photo.
2. **Brief and research.** Claude turns it into a purchase brief (item, quantity, deadline, budget, ship-to) and checks the market price on the web: low, typical, high.
3. **Supplier calls.** Fo calls each supplier for a quote: unit price, minimum order, lead time, shipping, fees.
4. **Compare.** Buyline ranks quotes by landed cost and flags traps (for example, the cheapest supplier whose minimum order is 1,000 m when you need 500).
5. **Negotiate.** Buyline picks the lever (a rival's quote, or the last price you paid, remembered with Cognee) and the number; Fo calls back to haggle. Your walk-away price is never shared with Fo.
6. **You decide.** Pick the recommendation or any other valid option. Approve, and Fo emails the purchase order. A PDF of the PO is ready to download. You can cancel a run or an order at any time.
7. **Morning briefing.** A short spoken summary (Deepgram Orpheus voice) of what got bought overnight.

## What to expect in this repo

**It runs in sample mode by default: no phone calls are made.** You can search for anything ("metal bars", "M8 bolts", "printer paper"). The supplier calls are simulated and clearly labelled "sample call", with prices scaled to your budget, so you can see the whole flow end to end in about a minute. Suppliers A, B and C are fictional (555 phone numbers, `.example` emails).

Live mode (`BUYLINE_MODE=live`) uses real Fo calls and is only set up for our own test phone; it needs Fo credentials that aren't included.

## Run it

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                                   # creates .venv with the dependencies
echo "ANTHROPIC_API_KEY=sk-..." > .env    # your own key; used for the brief, research and quote reading
cd buyline
../.venv/bin/python serve.py 8020
```

Open **http://localhost:8020/app.html**, type a request and press Start. When the decision sheet opens, approve, then download the PO.

Optional: `DEEPGRAM_API_KEY` in `.env` makes the morning briefing speak.

## How it's built

- `buyline/orchestrate.py`: the pipeline (brief, sourcing, calls, ranking, negotiation, PO)
- `buyline/agent.py`: supplier discovery and negotiation reasoning (Claude)
- `buyline/market.py`: web market-price check
- `buyline/fo.py`: Fo MCP client (`ask_fo`, `get_fo_reply`, `get_fo_conversation`), plus the sample/stub modes
- `buyline/memory.py`: supplier memory with Cognee
- `buyline/po_pdf.py`: the purchase order PDF (headless Chrome)
- `buyline/serve.py` + `buyline/app.html`: the local server and the product UI

More detail: [buyline/README.md](buyline/README.md).

## What's next

True overnight runs (start at 6 PM; call 24/7 distributor desks and overseas suppliers in their working hours, email the rest; best deal ready at 8 AM), finding new suppliers on its own, multi-item orders, and hooking into accounting and ERP.
