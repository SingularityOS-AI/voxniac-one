"""
scrapers/ — post-hackathon stub. Not built tonight, by explicit CEO decision.

Context (PLAN_FASE4_CAMPAIGNS.md, Etapa C): a Google Maps scraper was
prototyped earlier in this project's history and discarded — it fabricated
phone numbers and email addresses that don't exist (it does not have access
to that data at all, so it invented plausible-looking values), which
directly violates this app's non-negotiable rule that no fabricated contact
data ever reaches `leads.py`'s obfuscation pipeline. A paid Apollo API
integration is the planned real data source; it isn't wired yet.

Until a real source lands, `leads.py`'s only import path is
`POST /leads/import` (a human-provided Apollo CSV export) — see leads.py's
`import_csv()`. This package exists only to document the interface any
future scraper/importer module MUST implement so it plugs into leads.py
without changes there:

    def fetch_leads(**kwargs) -> list[dict]:
        '''Returns a list of RAW lead dicts, each with AT MOST these keys
        (any subset — never invent a key with a fabricated value):
          contactName: str
          companyName: str
          phone: str          # RAW, unmasked — leads.mask_phone() runs on
                               # this before it ever reaches leads.db
          email: str           # RAW, unmasked — leads.mask_email() runs on
                               # this before it ever reaches leads.db
          industry: str
          companySize: str
          seniority: str
          painPoints: list[str]

        Non-negotiable rule for any implementation of this interface: every
        value returned must come from a real, verifiable source (a paid API
        response, a CSV a human exported, etc.) — never a plausible-looking
        placeholder. If a field isn't available for a given lead, omit the
        key entirely; leads.py's import path already defaults missing
        fields to safe blanks. Fabricating "sample" data to fill a column is
        exactly the failure mode that got the Maps scraper discarded.
        '''
        raise NotImplementedError

No implementation exists in this package yet. Do not import this module
expecting a working scraper — it currently only documents the contract
above for whoever builds the first real one.
"""
