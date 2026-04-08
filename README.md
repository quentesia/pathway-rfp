# pathway-rfd

## TODOs

- Before sending emails, check DB for existing distributors in the area and fold them in; skip re-sending to any distributor already contacted for overlapping ingredients
- Check if one distributor covers most items except one or two — attempt to negotiate for the remaining items with that distributor before splitting the order
- Check whether a slightly higher unit price becomes cheaper after applying volume/mass discounts from a distributor already covering most items
- Clean database entry formats
- Write a README with setup instructions and record the Loom demo — required deliverables

## Would Improves

- Switch to Claude structured outputs / tool_use instead of JSON-in-text with fence stripping — eliminates the parse-and-validate loop
- Add texting for the phone-only companies
- Parse popularity of dishes through reviews on Google and Yelp as a starting point. Currently, claude infers popularity.
- Add a clear algorithm for analyzing the optimal order placement based on spoilage, freezer size, and distributor delivery, discounts and pricing.
- Better estimation of how many times each dish is ordered per week. Currently, takes 40 as an average for all dishes.
- Distributor search uses Google Places via Serper — results are non-deterministic across runs. Run multiple query variations per category ("wholesale distributor", "supplier", "market wholesale"), increase top-N, and cache/aggregate across runs to build coverage over time
- Change the fallback search to Perplexity (cheaper for simple searches, more powerful)
- Change the fallback form filling to an agentic browser automation tool like FillApp
