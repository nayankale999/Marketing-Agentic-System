# P2-PERS — Personalisation at Scale (outline)

**Status:** Phase 2 — shape-only. Not ready to build until E04 (audiences) and E06 (content) are stable in production.
**Diagram reference:** Intersection of P3 (Content Generation) and P4 (Audience Analysis)
**Depends on:** E04, E06, E10

Move from "one creative per campaign step, sent to everyone in the audience" (MVP) to "per-segment dynamic content, rendered at send time, measured with safe holdouts". Requires the `personalisation_rule` table introduced in `schema.sql`.

---

### P2-PERS-S01 — Define personalisation rules per asset

Marketer defines rules: `if segment matches X, render variation Y`. Rules carry a priority; the highest-priority match wins. Default fallback is the un-personalised content.

Open questions:
- How many rules per asset is too many before the UI becomes unusable?
- Should rules be expressed declaratively (UI builder) or as code (template snippets)?
- How do we preview the resulting content for an arbitrary recipient?

### P2-PERS-S02 — Render content at dispatch time

The Channel Distribution agent calls `personalisation.render(content_asset, audience_member)` per recipient at dispatch. Rendered content is cached per (asset, segment) for cost control.

Open questions:
- Cache TTL — recompute per send batch, or invalidate on rule change?
- What happens when a rule references missing data on a recipient?
- Per-recipient render is expensive at scale; is segment-level rendering sufficient for most cases?

### P2-PERS-S03 — Holdout-safe rollout

Personalisation rules are rolled out behind a holdout (default 10%). The non-personalised version is sent to the holdout; results compare uplift. Rules with no measurable uplift are surfaced for retirement.

Open questions:
- Tenant-level holdout, or per-campaign?
- How long must a rule run before retirement consideration?

### P2-PERS-S04 — Persona-aware Content Creator

When personalisation rules are defined, the Content Creator agent generates the base content + variations per rule in one task. The brief explicitly tells the agent which segments differ how.

Open questions:
- Should the agent propose new personalisation rules, or only honour those the marketer defined?
- Where does persona library live, and how does it relate to audience criteria (E04)?

### P2-PERS-S05 — Personalisation analytics

Per-rule reporting: which rules fired, on whom, with what outcome. Helps marketers retire rules that don't add value and double down on those that do.

Open questions:
- Default time window for "rule performance" view?
- How is rule attribution computed when multiple rules combine on the same recipient?
