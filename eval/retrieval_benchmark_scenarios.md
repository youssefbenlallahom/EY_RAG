# EY RAG Retrieval Benchmark Scenarios

This benchmark contains 20 gold scenarios for comparing retrieval quality across indexing types:

- `langchain_markdown_recursive`
- `llamaindex_semantic`
- `haystack_document_splitter`
- `chonkie_semantic`

Use `eval/retrieval_benchmark_scenarios.json` as the source of truth for automation. This Markdown file is a readable working guide for reviewing the scenario intent.

## Evaluation Principle

A strong retrieval result should return the correct source chunk(s), preserve numeric precision, include the relevant image for hybrid scenarios, and produce an answer grounded only in the retrieved evidence.

Suggested score:

| Component | Points |
| --- | ---: |
| Answer coverage | 40 |
| Source support | 25 |
| Image support | 20 |
| Precision and no hallucination | 15 |
| Total | 100 |

## Text-Only Scenarios

### T01 - CEO sentiment, GDP, and cost pressure

Question: In the January 2026 EY-Parthenon CEO Outlook, did CEO sentiment improve or soften, and what hard macro and cost-pressure signals explain the caution?

Ground truth: CEO sentiment softened from 83.0 to 78.5. Global real GDP growth was expected to ease from 3.3% in 2025 to 3.1% in 2026. In 2026, 61% of CEOs expected higher business costs and 16% expected reductions.

Evidence: `output_clean/ey-gl-ceo-outlook-survey-01-2026.md`, pages 3 and 5.

### T02 - Enterprise-wide transformation priorities

Question: What does the January 2026 CEO Outlook say about the state of enterprise-wide transformation and the top transformation outcomes CEOs are targeting?

Ground truth: 52% of CEOs were already undergoing enterprise-wide transformation and 45% planned to begin one in 2026. Top outcomes: operations/productivity including digitalization at 43%, customer engagement/retention at 40%, product/process innovation at 37%, and top-line growth at 36%.

Evidence: `output_clean/ey-gl-ceo-outlook-survey-01-2026.md`, pages 6-7.

### T03 - M&A, JVs, and alliances as transformation levers

Question: How are acquisitions, joint ventures, and strategic alliances positioned as transformation levers in the January 2026 CEO Outlook?

Ground truth: 53% of CEOs intended to pursue acquisitions in the next 12 months. 50% cited operational optimization and productivity gains as the primary acquisition objective. 79% planned JVs or strategic alliances in 2026, up from 62% in 2025.

Evidence: `output_clean/ey-gl-ceo-outlook-survey-01-2026.md`, pages 15-16.

### T04 - IFRS 18 effective date and transition

Question: For IFRS 18, what is the effective date, what does it replace, and what transition approach is required?

Ground truth: IFRS 18 replaces IAS 1. It is effective for reporting periods beginning on or after 1 January 2027. Earlier application is permitted and must be disclosed. It is applied retrospectively with reconciliation between IAS 1 and IFRS 18 presentation for the immediately preceding comparative period.

Evidence: `output_clean/ey-gl-applying-ifrs-a-closer-look-at-ifrs-18-07-2025.md`, pages 4-5 and 117.

### T05 - IFRS 18 categories and subtotals

Question: What five categories does IFRS 18 require for income and expenses in the statement of profit or loss, and what two additional subtotals support them?

Ground truth: Categories are operating, investing, financing, income taxes, and discontinued operations. Additional subtotals are operating profit or loss and profit or loss before financing and income tax.

Evidence: `output_clean/ey-gl-applying-ifrs-a-closer-look-at-ifrs-18-07-2025.md`, page 28.

### T06 - IFRS 18 management-defined performance measures

Question: Under IFRS 18, when is a performance measure an MPM, and what must the entity disclose about MPMs?

Ground truth: An MPM is a subtotal of income and expenses used in public communications outside financial statements to communicate management's view of an aspect of the entity's financial performance as a whole. IFRS 18 requires all MPM information in a single note, including purpose, usefulness, calculation, reconciliation to the most comparable IFRS subtotal, and related tax/NCI effects.

Evidence: `output_clean/ey-gl-applying-ifrs-a-closer-look-at-ifrs-18-07-2025.md`, pages 94 and 103.

### T07 - Good Group IFRS 18 profit or loss values

Question: In Good Group IFRS 18's 2025 consolidated statement of profit or loss, what are revenue, operating profit, profit before financing and income taxes, profit before income tax, and final profit?

Ground truth: Revenue EUR 179,058 thousand; operating profit EUR 10,216 thousand; profit before financing and income taxes EUR 12,498 thousand; profit before income tax EUR 11,088 thousand; final profit EUR 8,216 thousand.

Evidence: `output_clean/ey-sg-good-group-ifrs-18-12-2025.md`, page 12.

### T08 - CSRD Barometer sample

Question: What was the CSRD Barometer 2025 sample, timing, and mandate/voluntary composition?

Ground truth: 200 CSRD-compliant sustainability statements for FY2024, published and selected up to 28 March 2025. The sample had 191 PIEs and 9 non-PIEs. Of the PIEs, 94 were mandated under local jurisdictions, while 97 PIEs and 9 non-PIEs reported voluntarily.

Evidence: `output_clean/ey-gl-ey-csrd-barometer-05-2025.md`, pages 29-30.

### T09 - CSRD assurance findings

Question: What did the CSRD Barometer find about assurance levels, comparatives, independent assurance service providers, and qualified conclusions?

Ground truth: Limited assurance predominated at 89%; reasonable assurance on individual metrics was 11%; one German financial services company had reasonable assurance on the whole statement. 27 companies, or 14%, did not provide comparatives; 173, or 87%, provided partial comparatives; 48, or 24%, had those figures partially assured. One French company used an IASP. Two companies, or 1%, received qualified conclusions.

Evidence: `output_clean/ey-gl-ey-csrd-barometer-05-2025.md`, page 27.

### T10 - SEBI digital assurance draft circular

Question: What did the Assurance Eye April 2025 article say SEBI's digital assurance draft circular would require, who would be in scope, and by when?

Ground truth: SEBI's 03 February 2025 draft circular proposed a management statement and auditor/independent practitioner's report using external data repositories. It covered source amounts, book amounts, reconciling items, and management explanations. It would apply to the top 100 listed entities by market capitalization from FY2024-25, for periods ending on or after 31 March 2025, with reports due to stock exchanges by 31 July 2025 and each relevant year thereafter.

Evidence: `output_clean/ey-assurance-eye-reporting-insights-april-2025.md`, pages 24-25.

## Hybrid Text + Image Scenarios

### H01 - AI technologies driving transformation

Question: Using the January 2026 CEO Outlook AI figure, which AI technologies are driving transformation and what is the strategic interpretation of the ranking?

Ground truth: GenAI 54%, machine learning 45%, agentic AI 37%, physical AI 30%, NLP 27%. The ranking shows movement from isolated use cases toward integrated AI systems and more automated decision/workflow models.

Image: `filtered_images/ey-gl-ceo-outlook-survey-01-2026__p9__imageFile10__scoreNA.png`

### H02 - Three strategic AI questions

Question: A CEO asks for the three strategic AI questions from the January 2026 figure. What are they and what decision does each question force?

Ground truth: Is AI a core enterprise capability or experiments; where should we scale AI and what should stop; are we optimizing or reshaping the business.

Image: `filtered_images/ey-gl-ceo-outlook-survey-01-2026__p10__imageFile12__scoreNA.png`

### H03 - Self-made growth strategic questions

Question: From the January 2026 transformation page, what three strategic questions should CEOs ask to generate self-made growth under constrained external demand?

Ground truth: Next wave of growth under constrained demand; converting technology/talent confidence into productivity and value; capabilities and operating model to scale enterprise change.

Image: `filtered_images/ey-gl-ceo-outlook-survey-01-2026__p7__imageFile6__scoreNA.png`

### H04 - Geopolitical fragmentation questions

Question: The board is worried about geopolitical fragmentation. According to the January 2026 CEO figure, what three questions should management answer and what actions do those questions imply?

Ground truth: Use AI/data to manage geopolitical uncertainty; redesign global operating/digital models for fragmentation; act with conviction rather than delay.

Image: `filtered_images/ey-gl-ceo-outlook-survey-01-2026__p13__imageFile17__score3.png`

### H05 - IFRS 18 aggregated-items flowchart

Question: According to the IFRS 18 aggregated-items decision flowchart, how should an entity label an 'other' balance and decide whether to disclose more information?

Ground truth: Use a more informative label if available; otherwise use the most precise label possible. If aggregated items are not only immaterial, disclose material information. If they are only immaterial but the balance is large enough to raise user questions, provide further information; otherwise no further consideration is needed.

Image: `filtered_images/ey-gl-applying-ifrs-a-closer-look-at-ifrs-18-07-2025__p27__imageFile4__scoreNA.png`

### H06 - IFRS 18 profit or loss figure

Question: Using the IFRS 18 statement-of-profit-or-loss figure, explain which items sit in operating, investing, financing, income taxes, and discontinued operations, and name the new items highlighted by the figure.

Ground truth: Operating includes revenue through operating profit. Investing includes associates/JVs and profit before financing and income tax. Financing includes interest expenses. Income taxes includes income tax expense. Discontinued operations includes loss from discontinued operations. New items are operating profit and profit before financing and income tax.

Image: `filtered_images/ey-gl-applying-ifrs-a-closer-look-at-ifrs-18-07-2025__p29__imageFile5__scoreNA.png`

### H07 - CSRD Scope 3 GHG intensity chart

Question: In the CSRD Barometer Scope 3 GHG intensity chart, which sectors appear as the highest-intensity outliers and what ranges should the response report?

Ground truth: EM reaches roughly 25,000 t CO2e per million euro. CG has a major high-intensity cluster around 10,000-12,000. TC, RR, and IS are generally mostly below about 2,000.

Image: `filtered_images/ey-gl-ey-csrd-barometer-05-2025__p17__imageFile7__scoreNA.png`

### H08 - ISA for LCE risk assessment flowchart

Question: From the IAASB ISA for LCE risk assessment flowchart, reconstruct the main flow from planning information to audit responses.

Ground truth: The flow starts with planning/client/inquiry/understanding inputs, identifies financial statement and assertion level risks, determines significant risks and significant classes/assertions, decides whether to test controls, assesses control risk if applicable, documents and revises the assessment, evaluates ISA for LCE appropriateness, then proceeds to audit responses.

Image: `filtered_images/IAASB-2025-Handbook-Volume-2__p121__imageFile68__scoreNA.png`

### H09 - 2025 investment hotspots

Question: According to the January 2025 CEO Outlook investment-hotspots figure, what are the top five capital investment destinations and one risk or caveat for each?

Ground truth: United States, Canada, Germany, Mexico, United Kingdom. Risks include protectionist/monetary uncertainty for the US, US trade tensions and political uncertainty for Canada, political turmoil for Germany, tariff/protectionist pressure for Mexico, and uncertain trading relationships for the UK.

Image: `filtered_images/ey-ceo-outlook-pulse-survey-global-report-january-2025__p14__imageFile7__score3.png`

### H10 - Transformation leaders infographic

Question: Using the August/September 2025 CEO transformation-leaders infographic, compare transformation leaders with others on optimism, localization, regionalization, and portfolio strategy.

Ground truth: Leaders vs others: optimism past 12 months 56% vs 18%; next 12 months 71% vs 34%; completed localization 62% vs 34%; completed regionalization 58% vs 15%; increase investment 100% vs 45%; finance transformation through revenue/margin 88% vs 54%; M&A 57% vs 46%; divestment 47% vs 29%.

Image: `filtered_images/ey-gl-ceo-outlook-survey-08-2025__p3__imageFile6__score2.png`
