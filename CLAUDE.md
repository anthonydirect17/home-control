# Home Control: notes for Claude sessions

- **Read `docs/HANDOFF.md` first, all of it.**
- **Before any work, read the owner's `CHAIN_OF_COMMAND.md`** and the server's onboarding guide and box rules. They are in the owner's private shared folder, and they win over anything in this repo. A Home Control session leads its own project only:
  - anything on the server goes to the server's coordinator as a request card;
  - trading or cross-project questions go to the lead coordinator;
  - the owner gives every GO.
- **Ask instead of guessing.**
- **This repo is public.** No secrets, account names, addresses, device names or locations in code, docs, tests, logs or commits.
- **Never hard-code the server's Tailscale address.** The app reads it at every start (`--host tailscale`).
- **Never call anything that contacts Ring's monitoring center.** Arming, disarming and sirens need the page PIN every time.
- **Tests run without hardware:**
  - Python: `python -m pytest`
  - the Ring helper: `node --test` in `ring-bridge/`
