# Paper-Scraper
# Gene Therapy / Editing / Delivery / Engineering

Automated daily summary of relevant papers and Reddit discussions for gene therapy researchers.

## Overview

This script scrapes:
- **PubMed** (peer-reviewed papers)
- **bioRxiv** (preprints)
- **Reddit** (`r/labrats`, `r/bioinformatics`, `r/machinelearning`, `r/GeneEditing`, `r/clinicaltrials`, `r/biotech`)

It then:
- Filters for papers **4–5 weeks old** (to allow PubPeer comments to appear)
- Filters Reddit posts **1–8 days old** (to allow votes/comments to settle)
- Checks PubPeer (notes comments, never cancels)
- Sends all candidates to **DeepSeek API** for selection and 2–3 sentence summarisation
- Delivers a formatted daily digest via **Telegram**

## Search Keywords

- `"gene therapy"`
- `"gene editing"`
- `"gene delivery"`
- `"gen* engineering"`
- `"machine learning" AND biochem*`
- `"bioinformatics" AND biochem*`
- `"DNA repair"`
- `"epigenetic*"`

## Always-Include Authors

- David Liu (ORCID: 0000-0002-9943-7557)
- Ciaran Lee (ORCID: 0000-0002-0789-9149)

Papers from these authors are always included in the digest.

## Character Caps (Safe – Above Normal)

| Item | Cap |
| :--- | :--- |
| Abstract | 2000 chars |
| Reddit selftext | 3000 chars |
| Reddit comment | 500 chars |
| Reddit reply | 300 chars |

If a cap is exceeded, the system logs it but never truncates meaningful content.

## Why 4 Weeks for Papers?

PubPeer comments have a median delay of ~10 days, but can take up to several weeks. A 4–5 week window:
- Catches most meaningful comments
- Avoids sending papers before community reaction forms
- Still provides timely enough updates for a researcher

## Why 1–8 Days for Reddit?

Reddit votes and comments stabilise within 24–48 hours. The 1–8 day window:
- Skips very fresh posts where votes are still hidden
- Catches posts with established discussion
- Avoids stale content (>8 days)

## Setup

### 1. GitHub Repository

Create a new repository and push these files:
