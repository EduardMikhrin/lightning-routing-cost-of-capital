# Reproducible snapshot -> tables -> figure.
#
# PYTHON lets you point at a virtualenv without editing this file:
#   make snapshot PYTHON=.venv/bin/python
#   make snapshot PYTHON="uv run --with-requirements requirements.txt python"
PYTHON ?= python3

.PHONY: snapshot fetch tables clean-derived

# Full run: collect raw snapshots, then rebuild every derived table and the
# figure from them.
snapshot: fetch tables

fetch:
	$(PYTHON) scripts/fetch_mempool.py
	$(PYTHON) scripts/fetch_price.py
	$(PYTHON) scripts/fetch_amboss.py

tables:
	$(PYTHON) scripts/build_tables.py

# Derived artefacts only. Raw snapshots are never deleted by any make target:
# they are the evidence the paper's numbers rest on.
clean-derived:
	rm -f data/derived/*.csv data/derived/_sources.json
	rm -f figures/*.png figures/*.csv
