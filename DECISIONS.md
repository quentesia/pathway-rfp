# Engineering Decisions

This document summarizes the most important design choices in the repo, grouped by pipeline step, and why they are strong decisions for a take-home context.

## Step 1: Menu -> Recipes & Ingredients

### Decision: Schema-validated structured extraction
- **What**: Parse menu images into structured recipe + ingredient objects and validate with Pydantic.
- **Reasoning**: Prevents malformed AI output from silently polluting DB state.

### Decision: Menu dedup via content hash
- **What**: Use SHA-256 hash of menu image to avoid reparsing the same input.
- **Reasoning**: Saves latency and model/API cost in iterative demos.

### Decision: Canonical ingredient + category normalization
- **What**: Deduplicate ingredient names per restaurant and normalize categories/tags.
- **Reasoning**: Enables reliable downstream matching, filtering, and analytics.

## Step 2: Pricing Trends

### Decision: BLS data as practical market proxy
- **What**: Use BLS Average Price data for trend coverage, with explicit labeling.
- **Reasoning**: Delivers stable, broad price trend signal when USDA matching is brittle in practice.

### Decision: Persistent monthly cache
- **What**: Cache BLS series data by month in DB.
- **Reasoning**: Fast reruns and API resiliency for demos.

## Step 3: Find Local Distributors

### Decision: Multi-layer contact discovery
- **What**: Homepage email scrape -> contact page scan -> form detection -> LLM fallback.
- **Reasoning**: Handles real-world missing/fragmented contact data gracefully.

### Decision: Contact channel encoding
- **What**: Store contact forms as `form:<url>` in email field.
- **Reasoning**: Keeps schema lean while preserving actionable outreach channel.

### Decision: Category coverage mapping + tags
- **What**: Normalize distributor categories and map to ingredient demand categories.
- **Reasoning**: Supports step-level filtering and coverage diagnostics.

## Step 4: RFP Outreach

### Decision: Actual send path + safe dry-run path
- **What**: UI toggle for `Dry Run` (Yopmail) vs `Live` mode.
- **Reasoning**: Demonstrates production-capable path while enabling safe reviewer testing.

### Decision: Idempotent outreach status gating
- **What**: Send only to pending distributors.
- **Reasoning**: Prevents accidental duplicate outreach on reruns.

### Decision: Tiered demand model
- **What**: Replace flat per-dish covers with demand tiers + category factors + optional overrides.
- **Reasoning**: Reduces unrealistic quantity spikes and improves procurement credibility.
- **Later Improvement**: Use Google and Yelp reviews to gauge popularity and get a per-dish estimate.

## Step 5: Inbox Monitoring & Quote Comparison

### Decision: Autonomous clarification loop
- **What**: Parse replies, detect missing details, and send follow-up requests automatically.
- **Reasoning**: Moves beyond static parsing into operational procurement workflow.

### Decision: Omitted item handling
- **What**: Treat omitted ingredients as clarification-needed unless explicitly declined.
- **Reasoning**: Avoids false negatives that incorrectly mark suppliers as not carrying items.

### Decision: Procurement-context follow-up prompts
- **What**: Follow-ups request MOQ, bulk discounts, delivery lead times, payment terms.
- **Reasoning**: Aligns with actual buyer decision criteria, not just unit price.

### Decision: Dual operator views
- **What**: Step 5 supports `By Ingredient` and `By Provider` views with status signaling.
- **Reasoning**: Improves decision speed for both pricing and coverage/risk perspectives.

## Cross-Cutting Decisions

### Decision: LLM provider failover
- **What**: Anthropic primary, OpenAI backup-only fallback, with explicit provider logs.
- **Reasoning**: Increases reliability under transient provider/network failures.

### Decision: Streamed status updates
- **What**: `on_status` callbacks wire backend progress into UI statuses.
- **Reasoning**: Makes long-running steps transparent and demo-friendly.

### Decision: Explicit market-reference framing
- **What**: Compare quotes against `50% of BLS retail` as a labeled wholesale proxy.
- **Reasoning**: Reduces misleading interpretation while still offering directional context.

## Known Tradeoffs

- BLS is a proxy, not true wholesale distributor pricing.
- Distributor LLM fallback may return plausible but imperfect contact entities.
- Contact form automation does not solve CAPTCHAs. 
- Current demo is optimized for single-restaurant workflow execution.

## Future Improvements

- Switch to Claude structured outputs / tool_use instead of JSON-in-text fence stripping
- Add texting for phone-only companies
- Parse dish popularity from external signals (Google/Yelp reviews) instead of LLM inference alone
- Add a clearer optimization algorithm for final supplier split (spoilage, storage, discounts, delivery windows)
- Improve distributor search determinism by aggregating multiple query variants + caching over time
- Change fallback search provider to Perplexity for low-cost/simple lookups
- Replace fallback form-filling with an agentic browser automation tool like FillApp
- Add advanced neogtiations based on available alternative vendors and market prices
