Thumbstop Engine: raw outputs for the assignment show (50 episodes)

promos.json       The result. The 48 kept promos in posting order, each with its segments and timestamps, hook,
                  cliffhanger, why it works, why it is ranked there, the judge's scores (and percentile within the
                  show), and for the top 20 a fact-checked caption. After them, every other valid clip ("also found").
promos.csv        The same list, one row per promo.
report.md         A readable report of the ranked promos, the alternatives and the excluded clip.
candidates.jsonl  All 156 proposals, including the 18 that broke a rule after one repair, with the reasons.
excluded.json     The clip excluded for high ad-policy risk, and why.
reference.json    The show reference (characters, label map, episode summaries) the later stages used.
summary.json      Counts and the model used for each role.

Timestamps are mm:ss.xx within each episode.

These files were written by the final version of the code, which replayed every model answer from the run's
answer cache (offline mode). That is why summary.json shows near-zero minutes. Measured on NVIDIA's free tier, the
live stages took about 3 h 15 min: show reference 9 min, promo finding 26, montage 3, judging about 100,
ranking 7, captions 18, fact check 30.

Models: Nemotron 3 Ultra (show reference, promo finder, montage, ranker), Step 5 (judge),
Gemma 4 31B (captions), Mistral Large 4 with low reasoning effort (fact check).
