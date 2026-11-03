# Generating the complete guide

`CloudPulse_Complete_Guide.docx` (in `docs/`) is generated from the repository itself:
code excerpts are copied from the source files, and every number is read from the
committed results files, so the guide cannot drift from the code.

```bash
pip install matplotlib            # charts
sudo apt-get install graphviz     # diagrams (the `dot` command)
npm install docx                  # Word document builder
python3 docs/guide/diagrams.py    # architecture diagrams  -> docs/guide/img/
python3 docs/guide/charts.py      # result charts (reads results/ and the meta DB)
node docs/guide/main.js docs/CloudPulse_Complete_Guide.docx
```
