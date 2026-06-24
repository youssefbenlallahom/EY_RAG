# RAG Extraction Quality Report

Generated: 2026-06-22 12:40:14 +01:00

## Corpus Summary

| Metric | Value |
| --- | ---: |
| Documents evaluated | 16 |
| Total pages | 2414 |
| Pages with extracted text | 2395 (99.21%) |
| Sparse pages (< 50 chars) | 27 (1.12%) |
| Extracted text characters | 7928581 |
| Average text chars/page | 3284.4 |
| Text elements | 45242 |
| Image elements | 1368 |
| Missing image files referenced by JSON | 0 |
| Typed elements missing page number | 7142 (9.24%) |
| Typed elements missing valid bbox | 7142 (9.24%) |
| Text elements missing valid bbox | 0 (0%) |
| Replacement chars | 0 |
| Control chars | 0 |
| High-symbol text elements | 313 |
| Markdown page marker mismatches | 0 |
| Average document metadata score | 87.5 / 100 |
| Average text extraction score | 98.7 / 100 |

Top element types: list item=23918, paragraph=18821, table cell=9028, table row=7142, table=6609, list=6395, heading=2426, text block=1526, image=1368, caption=77
Typed elements missing page by type: table row=7142
Typed elements missing bbox by type: table row=7142

## Weakest Text Extraction Documents

| Document | Score | Coverage | Sparse pages | Avg chars/page | Missing text bbox | Worst sparse pages |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| ey-gl-ctools-ifrs-update-04-2025.pdf | 90.5 | 89.47% | 2 | 2536.6 | 0% | p1=0; p19=0 |
| en-gl-annual-ifrs-disclosure-checklist-v2-09-2025.pdf | 96.9 | 96.55% | 7 | 2863.4 | 0% | p1=0; p198=0; p199=0; p200=0; p201=0; p202=0; p203=0 |
| IAASB-2025-Handbook-Volume-5.pdf | 97 | 97.1% | 3 | 3385.3 | 0% | p2=0; p68=35; p69=0 |
| IAASB-2025-Handbook-Volume-2.pdf | 98.4 | 98.46% | 3 | 3022.8 | 0% | p2=0; p129=29; p130=0 |
| IAASB-2025-Handbook-Volume-3.pdf | 98.8 | 98.88% | 3 | 3684.1 | 0% | p2=0; p177=35; p178=0 |

## Weakest Metadata Documents

| Document | Metadata score | Title | Author | Creation date | Modification date | Source PDF |
| --- | ---: | --- | --- | --- | --- | --- |
| ey-ceo-outlook-pulse-survey-global-report-january-2025.pdf | 80 | False | False | True | True | True |
| ey-gl-applying-ifrs-a-closer-look-at-ifrs-18-07-2025.pdf | 80 | False | False | True | True | True |
| ey-gl-ceo-outlook-survey-01-2026.pdf | 80 | False | False | True | True | True |
| ey-gl-ceo-outlook-survey-08-2025.pdf | 80 | False | False | True | True | True |
| IAASB-2025-Handbook-Volume-2.pdf | 80 | False | False | True | True | True |

## Per-Document Summary

| Document | Pages | Text coverage | Sparse pages | Avg chars/page | Metadata | Text score | Grade |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| en-gl-annual-ifrs-disclosure-checklist-v2-09-2025.pdf | 203 | 96.55% | 7 | 2863.4 | 85 | 96.9 | excellent |
| ey-assurance-eye-reporting-insights-april-2025.pdf | 28 | 100% | 1 | 3328.5 | 95 | 98.9 | excellent |
| ey-ceo-outlook-pulse-survey-global-report-january-2025.pdf | 16 | 100% | 0 | 1915.8 | 80 | 100 | excellent |
| ey-frd-series-spring-2025.pdf | 51 | 100% | 0 | 797.1 | 95 | 100 | excellent |
| ey-gl-applying-ifrs-a-closer-look-at-ifrs-18-07-2025.pdf | 129 | 100% | 0 | 2806.7 | 80 | 100 | excellent |
| ey-gl-ceo-outlook-survey-01-2026.pdf | 18 | 100% | 0 | 2322.5 | 80 | 100 | excellent |
| ey-gl-ceo-outlook-survey-08-2025.pdf | 16 | 100% | 0 | 2361.4 | 80 | 100 | excellent |
| ey-gl-ctools-ifrs-update-04-2025.pdf | 19 | 89.47% | 2 | 2536.6 | 100 | 90.5 | excellent |
| ey-gl-ey-csrd-barometer-05-2025.pdf | 33 | 100% | 0 | 2100.2 | 100 | 100 | excellent |
| ey-gl-sustainability-deve-10-ed-sasb-ifrs-07-2025.pdf | 4 | 100% | 0 | 2513.5 | 100 | 100 | excellent |
| ey-sg-good-group-ifrs-18-12-2025.pdf | 172 | 100% | 1 | 3414.1 | 95 | 99.8 | excellent |
| IAASB-2025-Handbook-Volume-1.pdf | 1046 | 99.81% | 4 | 3510.2 | 85 | 99.8 | excellent |
| IAASB-2025-Handbook-Volume-2.pdf | 130 | 98.46% | 3 | 3022.8 | 80 | 98.4 | excellent |
| IAASB-2025-Handbook-Volume-3.pdf | 178 | 98.88% | 3 | 3684.1 | 85 | 98.8 | excellent |
| IAASB-2025-Handbook-Volume-4.pdf | 302 | 99.34% | 3 | 3551 | 80 | 99.3 | excellent |
| IAASB-2025-Handbook-Volume-5.pdf | 69 | 97.1% | 3 | 3385.3 | 80 | 97 | excellent |

## Interpretation

- A sparse page is not always a failure. Covers, divider pages, and image-heavy pages can be legitimate. For RAG, sparse pages should be inspected when they contain charts or tables that need OCR or image-to-text enrichment.
- Page number and bounding-box completeness are provenance signals. In this corpus, text elements have complete bounding boxes; the remaining missing provenance is concentrated in wrapper elements such as table rows.
- Metadata score is intentionally strict about author/date/title/source fields. A missing author is common in PDFs, but it lowers filtering and governance quality.

CSV details: output/rag_extraction_quality_summary.csv
