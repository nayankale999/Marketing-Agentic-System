# MAS — 60-second marketing video

A 60-second hero video for the Marketing Agentic System. Designed to be
shot/recorded against the `scripts/full_demo.py` terminal run + a small
set of UI screenshots, with a voiceover laid on top.

**Total runtime: 60s. ~135 words of VO at conversational pace (135 wpm).**

---

## Beat sheet

| Time      | Beat                          | What's on screen                                                  | What the VO says                                                                                       |
|-----------|-------------------------------|-------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| 0–5s      | Cold-open hook                | A marketer's overflowing inbox + 6 browser tabs (email, LinkedIn, SendGrid, Plausible, Google Sheets, Slack) | "Marketing teams now run a campaign across six tools — and spend half the week stitching them together." |
| 5–12s     | Cut to: MAS dashboard         | The campaign detail page, status pill `live`, one panel per agent | "MAS replaces the stitching. Six agents — one product."                                                |
| 12–22s    | Demo: brief → strategy        | Terminal: `scripts/full_demo.py` steps 3–5 scroll past             | "A brief lands. The Strategist proposes the channel mix, the Audience Targeting agent materialises the ICP." |
| 22–32s    | Demo: content + approval gate | Step 6 — content drafted, manager approves                         | "The Content Creator drafts copy with your brand voice. A manager approves with one click."            |
| 32–42s    | Demo: dispatch + A/B          | Step 10 — A/B test winner declared, p_value < 0.01                 | "Distribution sends across email and LinkedIn. The A/B test promotes the winner — automatically."     |
| 42–52s    | Demo: anomaly + recommendation| Steps 11 — anomaly flagged red; budget shift proposal in green    | "Analytics catches a critical unsubscribe spike in real time, and proposes a budget shift the marketer can accept in one click." |
| 52–58s    | Demo: end-of-campaign report  | Step 14 — report sections list on a tidy page                      | "End of campaign, the report writes itself. Objectives, KPIs, A/B outcomes, anomalies, spend — all reconciled." |
| 58–60s    | CTA                           | Logo + tagline "MAS — your marketing team's force multiplier"      | "MAS. Built for the team you already have."                                                            |

---

## Voiceover (full transcript)

> Marketing teams now run a campaign across six tools — and spend half
> the week stitching them together.
>
> MAS replaces the stitching. Six agents — one product.
>
> A brief lands. The Strategist proposes the channel mix, the Audience
> Targeting agent materialises the ICP.
>
> The Content Creator drafts copy with your brand voice. A manager
> approves with one click.
>
> Distribution sends across email and LinkedIn. The A/B test promotes
> the winner — automatically.
>
> Analytics catches a critical unsubscribe spike in real time, and
> proposes a budget shift the marketer can accept in one click.
>
> End of campaign, the report writes itself. Objectives, KPIs, A/B
> outcomes, anomalies, spend — all reconciled.
>
> **MAS. Built for the team you already have.**

---

## Production notes

**Capture order**
1. Record `scripts/full_demo.py` against a fresh testcontainer Postgres at 1080p, terminal font 18pt. Use `asciinema` or screen capture. The script prints structured sections with `─` rules that frame the cuts cleanly.
2. Screenshot the campaign detail page at `/ui/campaigns/<id>` (W32) — campaign in `live` status, approvals tab open.
3. Screenshot the end-of-campaign report at `/ui/campaigns/<id>/report` (W38).
4. Capture the anomaly + recommendation rows in the same page (or via the JSON endpoint highlighted in a code editor).

**Cuts**
- Use 0.3s crossfades between terminal segments to compress the demo.
- Add subtle "step ticks" — a soft chime when each `•` line appears — so the eye knows to refresh.

**Captions / lower thirds**
- 0:05 "Marketing Agentic System"
- 0:12 "Audience Targeting · Strategist · Content Creator"
- 0:32 "Approval · Distribution · Analytics & Optimisation"
- 0:55 "Built on FastAPI, Postgres, Claude · multi-tenant from day 1"

**AI-video generation prompts (alternative path)**

If you'd rather generate the broll programmatically (Sora / Runway / Veo)
instead of recording the demo, here are prompts that match each beat:

  0–5s — *"Stressed marketer in a startup office at her desk, six browser tabs glowing on her monitor, cinematic shot, shallow depth of field, late afternoon natural light."*

  5–12s — *"Animated diagram: six labeled agent boxes (Audience, Strategist, Content, Approval, Distribution, Analytics) connected by glowing lines around a central 'MAS' hub, dark mode UI aesthetic, clean motion graphic."*

  32–42s — *"Split-screen UI mockup: A/B test variants side by side, a 'WINNER' badge sweeps onto variant B, confetti restraint — single particle burst."*

  42–52s — *"Notification dashboard: a critical anomaly card slides in red, then a green 'budget shift proposal' card slides up below it, smooth dashboard interaction."*

  52–58s — *"A printed report being assembled section by section on a clean desk, top-down shot, papers settling into a single bound document."*

  58–60s — *"Logo reveal: 'MAS' wordmark in a soft cool-toned gradient, tagline fades in below, full black background."*
