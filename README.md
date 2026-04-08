# pathway-rfd

## TODOs

- Aggregate ingredient quantities across recipes × estimated weekly covers before sending RFP emails — currently sends per-serving amounts (e.g. "0.25 lb butter") which are useless for procurement
- Store processed `message_id`s in DB so Step 5 doesn't reprocess emails on reruns; add scheduled polling every 30 min during business hours ±2 hrs instead of manual trigger
- Fix ingredient global uniqueness: ingredients are deduped by name across all restaurants, so a second restaurant inherits the wrong `usda_id`/category. Scope dedup per-restaurant or separate canonical ingredients from per-restaurant usage
- Before sending emails, check DB for existing distributors in the area and fold them in; skip re-sending to any distributor already contacted for overlapping ingredients
- Check if one distributor covers most items except one or two — attempt to negotiate for the remaining items with that distributor before splitting the order
- Check whether a slightly higher unit price becomes cheaper after applying volume/mass discounts from a distributor already covering most items
- Clean database entry formats
- Update Step 5 UI to be clearer — show comparison table, best-price recommendation, and awaiting-clarification state more prominently
- Write a README with setup instructions and record the Loom demo — required deliverables

## Would Improves

- Switch to Claude structured outputs / tool_use instead of JSON-in-text with fence stripping — eliminates the parse-and-validate loop
- Distributor search uses Google Places via Serper — results are non-deterministic across runs. Run multiple query variations per category ("wholesale distributor", "supplier", "market wholesale"), increase top-N, and cache/aggregate across runs to build coverage over time
- Change the fallback search to Perplexity (cheaper for simple searches, more powerful)
- Change the fallback form filling to an agentic browser automation tool like FillApp
