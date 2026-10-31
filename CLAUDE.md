# Home Control: notes for Claude sessions

- **Read `docs/HANDOFF.md` first, all of it.**
- **Before any work, read the owner's `CHAIN_OF_COMMAND.md`** and the server's onboarding guide and box rules. They are in the owner's private shared folder, and they win over anything in this repo. A Home Control session leads its own project only:
  - anything on the server goes to the server's coordinator as a request card;
  - trading or cross-project questions go to the lead coordinator;
  - the owner gives every GO.
- **Ask instead of guessing.**
- **This repo is public.** No secrets, account names, addresses, device names or locations in code, docs, tests, logs or commits.
- **Never hard-code the server's Tailscale address.** The app reads it at every start (`--host tailscale`).
- **Never call anything that contacts Ring's monitoring center.**
- **The alarm and sirens** (the owner, 2026-10-31):
  - From the page, arming, disarming and sirens need the page PIN every time.
  - Arming and disarming from the app stays off until the owner turns its switch on with the PIN.
  - A saved rule may arm, change or disarm the alarm, or sound camera sirens, on its own. It needs the PIN every time it is saved or pressed by hand.
  - Rules never sound the base station siren.
- **Tests run without hardware:**
  - Python: `python -m pytest`
  - the Ring helper: `node --test` in `ring-bridge/`
