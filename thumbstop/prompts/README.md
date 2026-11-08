# Prompts: one system prompt per pipeline stage

The code loads these files through `thumbstop/prompts/__init__.py`. You can edit them without touching code. Two shared blocks are spliced in:
- `<<HOUSE>>` → `house_context.md`, which describes the promo desk, the brief's definition of a promo, and what works in microdrama ads;
- `<<HOOK_LINE_STYLE>>` → `hook_line_style.md`, the caption style guide.

This README is not sent to any model.

| Stage | File | Config role | Sees | Returns |
|---|---|---|---|---|
| 1 Show reference | `reference.md` | `reference` | Title, all speaker labels, the whole series (no camera lines; episode lengths and scene start times) | Premise, the title's promise, characters (role, relationships, secrets and the episode each is revealed), label map, episode summaries and peaks, arcs |
| 2 Promo finder | `propose.md` | `proposer` | Show reference up to Ep N+1, logline, Ep N with every line timed in seconds, the first 45 s of Ep N+1 | 0–5 promos as line-ID ranges, with hook, cliffhanger, why, funnel role, ad safety and its own measured length |
| 3 Rule-check repair | `repair.md` | `proposer` / `montage` | Measured segment and total lengths, the rule errors, code-computed fix options | Corrected promos (one retry; otherwise dropped) |
| 4 Montage | `montage.md` | `montage` | Show reference and a moment index: timed excerpts around every hook and cliffhanger found in stage 2 | 3–6 cross-episode montages |
| 5 Judge | `judge.md` | `judge` | One call per episode: the title and that episode's clips side by side (seconds from each ad's start, 3-s marker, cuts). No show knowledge | Scores per clip against fixed anchors, intrigue/confusion questions, ad-policy level, the best clip |
| 6 Ranker | `rank.md` | `ranker` | All kept promos as compact cards: episodes, length, cuts, funnel role, the actual opening and closing lines, judge scores and verdict, ad flag | Full posting order, with a "why" for each |
| 7 Captions | `captions.md` | `captions` | Characters by role, plus the top ranked promos' lines with IDs and loglines, in one batch | A main and an alt hook line per promo, with evidence |
| 8 Fact check | `factcheck.md` | `factcheck` | Captions, evidence lines, loglines, title, show facts | A verdict per claim, plus one suggested fix |

## Design principles
1. **The model chooses moments; code owns the timeline.** Every line is shown with its ID and its time in plain seconds, so the model can measure clip length. It answers with IDs, and code computes and snaps the real cuts. The model also reports its measured lengths. That forces a measure-don't-guess step; code compares the numbers with the real lengths but never trusts them.
2. **Each prompt is an agent brief:** role and audience, what it receives, the quality bar, traps seen in testing, a private step-by-step method, a checklist, and a strict JSON contract.
3. **Show-agnostic.** No prompt contains any show's names or plot. Examples use invented content and placeholder IDs (`E000-…`), so the tool behaves the same on a new show.
4. **Code verifies what code can verify:** length, the 3-s hook, mid-line cuts, caption length, names and spoiler evidence. The prompts state the same rules only so that fewer repairs are needed.
5. **The judge works blind, and nobody scores their own picks.** The promo finder returns no self-scores. The judge sees only the title and the clips, never the finder's reasoning. The judge runs on a different model family from the finder (Step 5 against Nemotron), and so does the fact checker against the caption writer (Mistral against Gemma). The ranker shares Nemotron with the finder, but it sees the judge's scores, not the finder's reasoning.

## Where the rules come from
| Rule | Reason |
|---|---|
| 20–60 s; hook in the first 3 s; cliffhanger; standalone; "usually one continuous stretch" | The assignment brief |
| Target 35–55 s; one segment by default; segments ≥ 6 s | In testing, models produced clips that were too short and stitched together 3–6 s fragments. Showing time explicitly and asking for measured lengths fixed both |
| "Start on the hook"; the weak-hook list | Proposals that started early (the hook landed after 3 s) and openings judged weak |
| "A reaction that settles the moment is not a cliffhanger" | Proposals that repeatedly ended on "her face hardens into resolve" |
| 0–5 promos; a calm episode may have none | Models padded calm episodes with weak promos |
| Caption rules: exact quotes, no upgraded facts, clear pronouns, no emojis, variety across the slate | Captions that misquoted, overstated ("proclaimed" → "crowned"), used emojis, or repeated one formula |
| Judge anchors, "most clips are 2–3", calm talk is not drama, the title is not required | Judges that inflated scores, rated calm scheming scenes highly, or docked clips that didn't show the title's premise |
| Fact check: generalising is fine, upgrading is false, threatened is not done, show facts for relationships, one fix | Checkers that were either too lenient or rejected true lines over wording |
| Ad-policy tiers | Platforms reject sexual content and graphic violence in ads. Excluded: sexual content (even implied), nudity, gore, torture shown, a child harmed. Ranked lower: on-screen violence or blood |
| Ranker: premise first, spread out storylines, a mild flag lowers position, random order × 3 | Variety matters in ad accounts, and LLM rankers favour whatever they read first |
