"""Regenerate the offline browser projection from the single knowledge source."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "src/g15/knowledge/maintenance.json"
target = ROOT / "src/g15/console/static/maintenance-catalog.js"
target.write_text(
    "// Generated from g15/knowledge/maintenance.json; do not edit separately.\n"
    "export const maintenanceCatalog = "
    + source.read_text(encoding="utf-8").rstrip()
    + ";\n",
    encoding="utf-8",
)
