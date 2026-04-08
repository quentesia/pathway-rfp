# pathway-rfd

## TODOs

- Add a script to automatically run the monitoring every half hour during business hours +- 2 hours.
- Show market averages in the monitoring (step 5) tab
- Clean database entry formats
- Before sending emails, check if we already have data. And also, for locations, always check the db if we have any already existing data about retailers in the area and add them to this restaurants distributor options if not already present, ask for any items we might need.
- Also check for if one of the other price points would be better after applying mass discounts.
- Check for if one distributor covers most things except one or two, and try to negotiate with that distributor for the remaining items.
- Update the UI a lot to be clearer towards the end

## Would Improves

- Change the fallback search to perplexity (generally cheaper for simple searches and more powerful).
- Change the fallback form filling to an agentic browser automation tool like FillApp.
- Distributor search uses Google Places via Serper — results are non-deterministic across runs, meaning some distributors may be missed on any given search. Production version would run multiple query variations per category (e.g. "wholesale distributor", "supplier", "market wholesale"), increase top-N results, and cache/aggregate results across runs to build more complete coverage over time.
