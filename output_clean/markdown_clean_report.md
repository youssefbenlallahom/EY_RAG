# Markdown Cleansing Report

Generated: 2026-06-24 11:01:11 UTC

## Corpus Summary

- Documents cleaned: 16
- Characters before: 8,645,666
- Characters after: 8,041,783
- Character reduction: 6.98%
- Image extractions integrated from cache: 63
- Duplicate paragraphs removed: 820
- Chart debris lines removed: 1214
- HTML tables converted: 1639
- Empty HTML tables removed: 0
- BR chart tables split: 0
- Incomplete extraction tables repaired: 1
- TOC lines cleaned: 363
- Bullets normalized: 5390
- False headings demoted: 39

## Page Types

- boilerplate: 5
- content: 2227
- cover: 14
- image_extraction: 81
- sparse: 24
- toc: 63

## Per-Document Summary

| Document | Pages | Chars before | Chars after | Reduction % | Integrated | Dupes removed | Debris lines | Tables converted |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| en-gl-annual-ifrs-disclosure-checklist-v2-09-2025 | 203 | 936774 | 603998 | 35.52 | 0 | 0 | 0 | 295 |
| ey-assurance-eye-reporting-insights-april-2025 | 28 | 100465 | 96544 | 3.9 | 0 | 27 | 9 | 9 |
| ey-ceo-outlook-pulse-survey-global-report-january-2025 | 16 | 44272 | 31635 | 28.54 | 0 | 34 | 141 | 1 |
| ey-frd-series-spring-2025 | 51 | 75209 | 65678 | 12.67 | 24 | 390 | 405 | 14 |
| ey-gl-applying-ifrs-a-closer-look-at-ifrs-18-07-2025 | 129 | 375777 | 368671 | 1.89 | 12 | 85 | 0 | 46 |
| ey-gl-ceo-outlook-survey-01-2026 | 18 | 48014 | 39088 | 18.59 | 7 | 98 | 287 | 2 |
| ey-gl-ceo-outlook-survey-08-2025 | 16 | 39603 | 41385 | -4.5 | 9 | 120 | 119 | 0 |
| ey-gl-ctools-ifrs-update-04-2025 | 19 | 49527 | 52700 | -6.41 | 1 | 0 | 0 | 1 |
| ey-gl-ey-csrd-barometer-05-2025 | 33 | 81717 | 75617 | 7.46 | 2 | 21 | 237 | 13 |
| ey-gl-sustainability-deve-10-ed-sasb-ifrs-07-2025 | 4 | 10441 | 10413 | 0.27 | 0 | 0 | 0 | 1 |
| ey-sg-good-group-ifrs-18-12-2025 | 172 | 600790 | 597074 | 0.62 | 0 | 0 | 0 | 3 |
| IAASB-2025-Handbook-Volume-1 | 1046 | 3837853 | 3678385 | 4.16 | 0 | 0 | 0 | 780 |
| IAASB-2025-Handbook-Volume-2 | 130 | 412582 | 400924 | 2.83 | 1 | 0 | 0 | 102 |
| IAASB-2025-Handbook-Volume-3 | 178 | 683879 | 670978 | 1.89 | 1 | 4 | 0 | 158 |
| IAASB-2025-Handbook-Volume-4 | 302 | 1107558 | 1071332 | 3.27 | 2 | 13 | 5 | 188 |
| IAASB-2025-Handbook-Volume-5 | 69 | 241205 | 237361 | 1.59 | 4 | 28 | 11 | 26 |

## Notes

- Cleansed files are written to `output_clean/`; originals in `output/` are untouched.
- Pages are tagged with `<!-- PAGE_TYPE: ... -->` for downstream filtering.
- Image extraction blocks are deduplicated against nearby chart debris and duplicate narrative text.

CSV details: `markdown_clean_report.csv`
