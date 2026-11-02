# Frozen BERT arXiv classification

Open `arxiv_frozen_bert.ipynb` and run all cells using a Python environment installed from `requirements.txt`.

The included `data/arxiv_cs.json` contains title, abstract, arXiv URL, and primary category for 217 papers. It was obtained on October 27, 2026 from https://export.arxiv.org/api/query using `search_query=cat:cs.CL`, `cat:cs.CV`, and `cat:cs.CR`, with `start=0`, `max_results=100`, `sortBy=submittedDate`, and `sortOrder=descending`. Only records whose primary category matched the query were retained; duplicate title/abstract pairs were removed. This is an API sample, not the full Cornell/Kaggle arXiv snapshot. Respect the original papers' licenses when redistributing text.

The pretrained BERT checkpoint is pinned in the notebook. BERT runs in evaluation/inference mode with all parameters frozen. Final-layer CLS embeddings feed a separate standardized logistic-regression head. Validation chooses C; a held-out test set evaluates the selected model. SHA256 fingerprints and gradient assertions verify that BERT stayed unchanged.
