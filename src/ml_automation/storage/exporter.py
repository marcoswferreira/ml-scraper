"""Export scraped products and generated affiliate links to JSON or CSV."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel


def export_items(
    items: Sequence[BaseModel],
    output_path: Path,
    fmt: Literal["json", "csv"] = "json",
) -> Path:
    """Serialize a sequence of Pydantic models to JSON or CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = [item.model_dump(mode="json") for item in items]

    if fmt == "json":
        output_path.write_text(
            json.dumps(serialized, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return output_path

    if fmt == "csv":
        fieldnames = list(serialized[0].keys()) if serialized else []
        with output_path.open("w", newline="", encoding="utf-8") as fh:
            if fieldnames:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(serialized)
        return output_path

    raise ValueError(f"Formato não suportado: {fmt}")
